/*
 * SoundTouch JS audio processing library
 * Copyright (c) Olli Parviainen
 * Copyright (c) Ryan Berdeen
 * Copyright (c) Jakub Fiala
 * Copyright (c) Steve 'Cutter' Blades
 *
 * Licensed under the Mozilla Public License, v. 2.0.
 * You can obtain one at https://mozilla.org/MPL/2.0/.
 */
import AbstractSamplePipe from './AbstractSamplePipe.js';
import CircularSampleBuffer from './CircularSampleBuffer.js';
import FifoSampleBuffer from './FifoSampleBuffer.js';
/**
 * Read adapter optimized for FIFO-backed buffers with a generic fallback path.
 */
class FifoStretchBufferAdapter {
    buffer;
    fallbackBuffer;
    fallbackScratch;
    constructor() {
        this.buffer = null;
        this.fallbackBuffer = new FifoSampleBuffer();
        this.fallbackScratch = new Float32Array(0);
    }
    /**
     * @param buffer Source buffer to expose through FIFO-style reads.
     */
    setBuffer(buffer) {
        if (buffer instanceof FifoSampleBuffer) {
            this.buffer = buffer;
            return;
        }
        const frameCount = buffer.frameCount;
        if (frameCount > 0) {
            const sampleCount = frameCount * 2;
            if (this.fallbackScratch.length < sampleCount) {
                this.fallbackScratch = new Float32Array(sampleCount);
            }
            buffer.extract(this.fallbackScratch, 0, frameCount);
            this.fallbackBuffer.clear();
            this.fallbackBuffer.putSamples(this.fallbackScratch, 0, frameCount);
            buffer.receive(frameCount);
        }
        else {
            this.fallbackBuffer.clear();
        }
        this.buffer = this.fallbackBuffer;
    }
    /**
     * Returns the currently bound FIFO buffer.
     * @throws Error when `setBuffer` has not been called yet.
     */
    getBoundBuffer() {
        if (this.buffer === null) {
            throw new Error('buffer is not set');
        }
        return this.buffer;
    }
    get frameCount() {
        return this.getBoundBuffer().frameCount;
    }
    get startIndex() {
        return this.getBoundBuffer().startIndex;
    }
    readSample(sampleIndex) {
        const boundBuffer = this.getBoundBuffer();
        const start = boundBuffer.startIndex;
        const end = start + boundBuffer.frameCount * 2;
        if (sampleIndex < start || sampleIndex >= end) {
            return 0;
        }
        return boundBuffer.vector[sampleIndex];
    }
    readSubarray(start, end) {
        return this.getBoundBuffer().vector.subarray(start, end);
    }
    receive(numFrames) {
        this.getBoundBuffer().receive(numFrames);
    }
    receiveSamples(output, numFrames) {
        this.getBoundBuffer().receiveSamples(output, numFrames);
    }
}
class GenericStretchWriteBufferAdapter {
    buffer;
    constructor() {
        this.buffer = null;
    }
    setOutputBuffer(buffer) {
        this.buffer = buffer;
    }
    /**
     * Returns the currently bound output buffer.
     * @throws Error when `setOutputBuffer` has not been called.
     */
    getBoundBuffer() {
        if (this.buffer === null) {
            throw new Error('output buffer is not set');
        }
        return this.buffer;
    }
    appendSamples(samples, numFrames) {
        this.getBoundBuffer().putSamples(samples, 0, numFrames);
    }
    putFrom(source, position, numFrames) {
        const sourceStart = source.startIndex + position * 2;
        const sourceEnd = sourceStart + numFrames * 2;
        const chunk = source.readSubarray(sourceStart, sourceEnd);
        this.getBoundBuffer().putSamples(chunk, 0, numFrames);
    }
}
class CircularStretchInputBufferAdapter {
    circularBuffer;
    rangeScratch;
    constructor() {
        this.circularBuffer = new CircularSampleBuffer();
        this.rangeScratch = new Float32Array(0);
    }
    /**
     * Binds a source buffer and stages its readable frames into the internal
     * circular storage.
     *
     * @param buffer Source buffer to import.
     */
    setBuffer(buffer) {
        if (buffer instanceof FifoSampleBuffer) {
            const frames = buffer.frameCount;
            if (frames > 0) {
                this.circularBuffer.pushSamples(buffer.vector, buffer.position, frames);
                buffer.receive(frames);
            }
            return;
        }
        const frames = buffer.frameCount;
        if (frames > 0) {
            const sampleCount = frames * 2;
            if (this.rangeScratch.length < sampleCount) {
                this.rangeScratch = new Float32Array(sampleCount);
            }
            buffer.extract(this.rangeScratch, 0, frames);
            this.circularBuffer.pushSamples(this.rangeScratch, 0, frames);
            buffer.receive(frames);
        }
    }
    get frameCount() {
        return this.circularBuffer.frameCount;
    }
    get startIndex() {
        return 0;
    }
    readSample(sampleIndex) {
        return this.circularBuffer.readSample(sampleIndex);
    }
    /**
     * Returns a contiguous range from circular storage, padding trailing values
     * with zeros when the requested range extends past available data.
     */
    readSubarray(start, end) {
        const normalizedStart = Math.max(0, Math.floor(start));
        const normalizedEnd = Math.max(normalizedStart, Math.floor(end));
        const requestedSamples = normalizedEnd - normalizedStart;
        const requestedFrames = Math.floor(requestedSamples / 2);
        if (requestedFrames <= 0) {
            return this.rangeScratch.subarray(0, 0);
        }
        const needed = requestedFrames * 2;
        if (this.rangeScratch.length < needed) {
            this.rangeScratch = new Float32Array(needed);
        }
        const sourceFrameOffset = Math.floor(normalizedStart / 2);
        const readFrames = this.circularBuffer.extract(this.rangeScratch, sourceFrameOffset, requestedFrames, false);
        const readSamples = readFrames * 2;
        if (readSamples < needed) {
            this.rangeScratch.fill(0, readSamples, needed);
        }
        return this.rangeScratch.subarray(0, needed);
    }
    receive(numFrames) {
        this.circularBuffer.dropFrames(numFrames);
    }
    receiveSamples(output, numFrames) {
        this.circularBuffer.extract(output, 0, numFrames, true);
    }
}
/**
 * Creates a stretch input adapter that reads from FIFO-compatible buffers.
 */
export const createFifoStretchInputBufferAdapter = () => new FifoStretchBufferAdapter();
/**
 * Creates a stretch input adapter backed by `CircularSampleBuffer`.
 */
export const createCircularStretchInputBufferAdapter = () => new CircularStretchInputBufferAdapter();
const USE_AUTO_SEQUENCE_LEN = 0;
const DEFAULT_SEQUENCE_MS = USE_AUTO_SEQUENCE_LEN;
const USE_AUTO_SEEKWINDOW_LEN = 0;
const DEFAULT_SEEKWINDOW_MS = USE_AUTO_SEEKWINDOW_LEN;
const DEFAULT_OVERLAP_MS = 8;
const AUTOSEQ_TEMPO_LOW = 0.25;
const AUTOSEQ_TEMPO_TOP = 4.0;
const AUTOSEQ_AT_MIN = 125.0;
const AUTOSEQ_AT_MAX = 50.0;
const AUTOSEQ_K = (AUTOSEQ_AT_MAX - AUTOSEQ_AT_MIN) / (AUTOSEQ_TEMPO_TOP - AUTOSEQ_TEMPO_LOW);
const AUTOSEQ_C = AUTOSEQ_AT_MIN - AUTOSEQ_K * AUTOSEQ_TEMPO_LOW;
const AUTOSEEK_AT_MIN = 25.0;
const AUTOSEEK_AT_MAX = 15.0;
const AUTOSEEK_K = (AUTOSEEK_AT_MAX - AUTOSEEK_AT_MIN) / (AUTOSEQ_TEMPO_TOP - AUTOSEQ_TEMPO_LOW);
const AUTOSEEK_C = AUTOSEEK_AT_MIN - AUTOSEEK_K * AUTOSEQ_TEMPO_LOW;
const NORMALIZED_CORRELATION_EPSILON = 1e-12;
const QUICK_SEEK_FALLBACK_THRESHOLD = 256;
const QUICK_SEEK_MIN_VALID_CANDIDATES = 8;
/**
 * Time-stretch processor for tempo adjustment without affecting pitch.
 * Used internally by SoundTouch for time-stretching audio.
 */
export default class Stretch extends AbstractSamplePipe {
    inputBufferAdapterFactory;
    sampleBufferFactory;
    inputBufferAdapter;
    outputBufferAdapter;
    overlapScratch;
    _quickSeek;
    midBufferDirty;
    midBuffer;
    refMidBuffer;
    refMidBufferEnergy;
    overlapLength;
    autoSeqSetting;
    autoSeekSetting;
    _tempo;
    sampleRate;
    _overlapMs;
    sequenceMs;
    seekWindowMs;
    seekWindowLength;
    seekLength;
    nominalSkip;
    skipFract;
    sampleReq;
    /**
     * Creates a Stretch instance.
     * @param options Constructor options.
     */
    constructor({ createBuffers = false, inputBufferAdapterFactory = createFifoStretchInputBufferAdapter, sampleBufferFactory = () => new FifoSampleBuffer(), } = {}) {
        super({
            createBuffers,
            inputBufferFactory: sampleBufferFactory,
            outputBufferFactory: sampleBufferFactory,
        });
        this.inputBufferAdapterFactory = inputBufferAdapterFactory;
        this.sampleBufferFactory = sampleBufferFactory;
        this.inputBufferAdapter = inputBufferAdapterFactory();
        this.outputBufferAdapter = new GenericStretchWriteBufferAdapter();
        this.overlapScratch = new Float32Array(0);
        this._quickSeek = true;
        this.midBufferDirty = true;
        this.midBuffer = null;
        this.refMidBufferEnergy = 0;
        this.overlapLength = 0;
        this.autoSeqSetting = true;
        this.autoSeekSetting = true;
        this._tempo = 1;
        this.setParameters(44100, DEFAULT_SEQUENCE_MS, DEFAULT_SEEKWINDOW_MS, DEFAULT_OVERLAP_MS);
    }
    clear() {
        super.clear();
        this.clearMidBuffer();
    }
    clearMidBuffer() {
        this.midBufferDirty = true;
        if (this.midBuffer) {
            this.midBuffer.fill(0);
        }
        if (this.refMidBuffer) {
            this.refMidBuffer.fill(0);
        }
        this.skipFract = 0;
    }
    setParameters(sampleRate, sequenceMs, seekWindowMs, overlapMs) {
        if (sampleRate > 0) {
            this.sampleRate = sampleRate;
        }
        if (overlapMs > 0) {
            this._overlapMs = overlapMs;
        }
        if (sequenceMs > 0) {
            this.sequenceMs = sequenceMs;
            this.autoSeqSetting = false;
        }
        else {
            this.autoSeqSetting = true;
        }
        if (seekWindowMs > 0) {
            this.seekWindowMs = seekWindowMs;
            this.autoSeekSetting = false;
        }
        else {
            this.autoSeekSetting = true;
        }
        this.calculateSequenceParameters();
        this.calculateOverlapLength(this._overlapMs);
        this.updateTempoDerivedState();
    }
    set tempo(newTempo) {
        this._tempo = newTempo;
        this.updateTempoDerivedState();
    }
    get tempo() {
        return this._tempo;
    }
    get inputChunkSize() {
        return this.sampleReq;
    }
    get outputChunkSize() {
        return (this.overlapLength +
            Math.max(0, this.seekWindowLength - 2 * this.overlapLength));
    }
    calculateOverlapLength(overlapInMsec = 0) {
        let newOvl = (this.sampleRate * overlapInMsec) / 1000;
        newOvl = newOvl < 16 ? 16 : newOvl;
        // must be divisible by 8
        newOvl -= newOvl % 8;
        if (newOvl === this.overlapLength && this.midBuffer !== null) {
            return;
        }
        this.overlapLength = newOvl;
        const needed = this.overlapLength * 2;
        if (!this.refMidBuffer || this.refMidBuffer.length < needed) {
            this.refMidBuffer = new Float32Array(needed);
        }
        if (!this.midBuffer || this.midBuffer.length < needed) {
            this.midBuffer = new Float32Array(needed);
        }
    }
    checkLimits(x, mi, ma) {
        return x < mi ? mi : x > ma ? ma : x;
    }
    calculateSequenceParameters() {
        if (this.autoSeqSetting) {
            let seq = AUTOSEQ_C + AUTOSEQ_K * this._tempo;
            seq = this.checkLimits(seq, AUTOSEQ_AT_MAX, AUTOSEQ_AT_MIN);
            this.sequenceMs = Math.floor(seq + 0.5);
        }
        if (this.autoSeekSetting) {
            let seek = AUTOSEEK_C + AUTOSEEK_K * this._tempo;
            seek = this.checkLimits(seek, AUTOSEEK_AT_MAX, AUTOSEEK_AT_MIN);
            this.seekWindowMs = Math.floor(seek + 0.5);
        }
        this.seekWindowLength = Math.floor((this.sampleRate * this.sequenceMs) / 1000);
        this.seekLength = Math.floor((this.sampleRate * this.seekWindowMs) / 1000);
        this.normalizeWindowInvariants();
    }
    normalizeWindowInvariants() {
        this.seekLength = Math.max(1, this.seekLength);
        this.seekWindowLength = Math.max(this.seekWindowLength, this.overlapLength);
    }
    updateTempoDerivedState() {
        this.calculateSequenceParameters();
        this.nominalSkip =
            this._tempo * (this.seekWindowLength - this.overlapLength);
        this.skipFract = 0;
        const intskip = Math.floor(this.nominalSkip + 0.5);
        this.sampleReq =
            Math.max(intskip + this.overlapLength, this.seekWindowLength) +
                this.seekLength;
    }
    /**
     * Whether the fast multi-pass seek algorithm is active.
     * @returns `true` if quick seek is enabled (default); `false` for exhaustive search.
     */
    get quickSeek() {
        return this._quickSeek;
    }
    set quickSeek(enable) {
        this._quickSeek = enable;
    }
    /**
     * Current overlap crossfade length in milliseconds.
     * @returns The overlap period used at the current sample rate.
     */
    get overlapMs() {
        return this._overlapMs;
    }
    /**
     * Sets the overlap crossfade length and recalculates derived parameters.
     * @param ms Overlap period in milliseconds (must be > 0).
     */
    set overlapMs(ms) {
        if (ms > 0) {
            this._overlapMs = ms;
            this.calculateOverlapLength(this._overlapMs);
            this.calculateSequenceParameters();
            this.updateTempoDerivedState();
        }
    }
    /**
     * Applies a partial set of WSOLA timing parameters.
     *
     * @remarks
     * Only the provided fields are updated; omitted fields remain unchanged.
     * Pass `sequenceMs: 0` or `seekWindowMs: 0` to switch that dimension back to auto-calculation.
     *
     * @param params Partial set of WSOLA timing parameters to apply.
     *
     * @example
     * stretch.setStretchParameters({ overlapMs: 12, quickSeek: false });
     */
    setStretchParameters(params) {
        if (params.quickSeek !== undefined) {
            this._quickSeek = params.quickSeek;
        }
        let needsRecalc = false;
        if (params.sequenceMs !== undefined) {
            if (params.sequenceMs > 0) {
                this.sequenceMs = params.sequenceMs;
                this.autoSeqSetting = false;
            }
            else {
                this.autoSeqSetting = true;
            }
            needsRecalc = true;
        }
        if (params.seekWindowMs !== undefined) {
            if (params.seekWindowMs > 0) {
                this.seekWindowMs = params.seekWindowMs;
                this.autoSeekSetting = false;
            }
            else {
                this.autoSeekSetting = true;
            }
            needsRecalc = true;
        }
        if (params.overlapMs !== undefined && params.overlapMs > 0) {
            this._overlapMs = params.overlapMs;
            this.calculateOverlapLength(this._overlapMs);
            needsRecalc = true;
        }
        if (needsRecalc) {
            this.calculateSequenceParameters();
            this.updateTempoDerivedState();
        }
    }
    clone() {
        const result = new Stretch({
            createBuffers: false,
            inputBufferAdapterFactory: this.inputBufferAdapterFactory,
            sampleBufferFactory: this.sampleBufferFactory,
        });
        result.tempo = this._tempo;
        result.setParameters(this.sampleRate, this.sequenceMs, this.seekWindowMs, this._overlapMs);
        return result;
    }
    seekBestOverlapPosition(inputBuffer) {
        const resolvedInputBuffer = inputBuffer ?? this.getInputBufferAdapter();
        if (!this._quickSeek || this.seekLength <= QUICK_SEEK_FALLBACK_THRESHOLD) {
            return this.seekBestOverlapPositionStereo(resolvedInputBuffer);
        }
        return this.seekBestOverlapPositionStereoQuick(resolvedInputBuffer);
    }
    seekBestOverlapPositionStereo(inputBuffer) {
        let bestOffset;
        let bestCorrelation;
        let correlation;
        this.preCalculateCorrelationReferenceStereo();
        bestOffset = 0;
        bestCorrelation = -Infinity;
        for (let i = 0; i < this.seekLength; i++) {
            correlation = this.calculateCrossCorrelationStereo(2 * i, this.refMidBuffer, inputBuffer);
            if (correlation > bestCorrelation) {
                bestCorrelation = correlation;
                bestOffset = i;
            }
        }
        return bestOffset;
    }
    seekBestOverlapPositionStereoQuick(inputBuffer) {
        let bestOffset;
        let bestCorrelation;
        let correlation;
        let correlationOffset;
        let tempOffset;
        let evaluatedCandidates;
        this.preCalculateCorrelationReferenceStereo();
        bestCorrelation = this.calculateCrossCorrelationStereo(0, this.refMidBuffer, inputBuffer);
        evaluatedCandidates = 1;
        bestOffset = 0;
        correlationOffset = 0;
        for (let scanCount = 0; scanCount < 4; scanCount++) {
            let previousTempOffset = Number.MIN_SAFE_INTEGER;
            const scanOffsets = this.getQuickScanOffsets(scanCount);
            for (const scanOffset of scanOffsets) {
                tempOffset = correlationOffset + scanOffset;
                if (tempOffset === previousTempOffset) {
                    continue;
                }
                previousTempOffset = tempOffset;
                if (tempOffset < 0) {
                    continue;
                }
                if (tempOffset >= this.seekLength) {
                    continue;
                }
                correlation = this.calculateCrossCorrelationStereo(2 * tempOffset, this.refMidBuffer, inputBuffer);
                evaluatedCandidates++;
                if (correlation > bestCorrelation) {
                    bestCorrelation = correlation;
                    bestOffset = tempOffset;
                }
            }
            correlationOffset = bestOffset;
        }
        if (evaluatedCandidates < QUICK_SEEK_MIN_VALID_CANDIDATES) {
            return this.seekBestOverlapPositionStereo(inputBuffer);
        }
        return bestOffset;
    }
    getQuickScanOffsets(stage) {
        const maxOffset = Math.max(1, this.seekLength - 1);
        if (stage === 0) {
            return this.generateFractionalScanOffsets(maxOffset, 2, 1, 14, 24);
        }
        if (stage === 1) {
            return this.generateSymmetricScanOffsets(maxOffset, 0.2);
        }
        if (stage === 2) {
            return this.generateSymmetricScanOffsets(maxOffset, 0.06);
        }
        return this.generateSymmetricScanOffsets(maxOffset, 0.015);
    }
    generateFractionalScanOffsets(maxOffset, startNumerator, stepNumerator, denominator, steps) {
        const offsets = [];
        const seen = new Set();
        const safeDenominator = Math.max(1, denominator);
        const safeSteps = Math.max(1, steps);
        for (let i = 0; i < safeSteps; i++) {
            const numerator = startNumerator + i * stepNumerator;
            const value = Math.round((maxOffset * numerator) / safeDenominator);
            if (value <= 0 || value >= this.seekLength || seen.has(value)) {
                continue;
            }
            seen.add(value);
            offsets.push(value);
        }
        return offsets;
    }
    generateSymmetricScanOffsets(maxOffset, spanRatio) {
        const span = Math.max(1, Math.round(maxOffset * spanRatio));
        const scales = [1, 0.75, 0.5, 0.25];
        const negative = [];
        const positive = [];
        const seen = new Set();
        for (const scale of scales) {
            const magnitude = Math.max(1, Math.round(span * scale));
            const neg = -magnitude;
            const pos = magnitude;
            if (!seen.has(neg)) {
                seen.add(neg);
                negative.push(neg);
            }
            if (!seen.has(pos)) {
                seen.add(pos);
                positive.push(pos);
            }
        }
        return negative.concat(positive);
    }
    preCalculateCorrelationReferenceStereo() {
        let energy = 0;
        for (let i = 0; i < this.overlapLength; i++) {
            const temp = i * (this.overlapLength - i);
            const ctx = i * 2;
            const left = this.midBuffer[ctx] * temp;
            const right = this.midBuffer[ctx + 1] * temp;
            this.refMidBuffer[ctx] = left;
            this.refMidBuffer[ctx + 1] = right;
            energy += left * left + right * right;
        }
        this.refMidBufferEnergy = energy;
    }
    calculateCrossCorrelationStereo(mixingPos, compare, inputBuffer) {
        mixingPos += inputBuffer.startIndex;
        let dot = 0;
        let sourceEnergy = 0;
        const calcLength = 2 * this.overlapLength;
        const source = inputBuffer.readSubarray(mixingPos, mixingPos + calcLength);
        for (let i = 0; i < calcLength; i += 2) {
            const sourceLeft = i < source.length ? source[i] : 0;
            const sourceRight = i + 1 < source.length ? source[i + 1] : 0;
            const compareLeft = compare[i];
            const compareRight = compare[i + 1];
            dot += sourceLeft * compareLeft + sourceRight * compareRight;
            sourceEnergy += sourceLeft * sourceLeft + sourceRight * sourceRight;
        }
        if (sourceEnergy <= NORMALIZED_CORRELATION_EPSILON ||
            this.refMidBufferEnergy <= NORMALIZED_CORRELATION_EPSILON) {
            return -1;
        }
        return dot / Math.sqrt(sourceEnergy * this.refMidBufferEnergy);
    }
    overlapStereo(inputPosition, inputBuffer, outputBuffer) {
        inputPosition += inputBuffer.startIndex;
        const overlapSamples = this.overlapLength * 2;
        if (this.overlapScratch.length < overlapSamples) {
            this.overlapScratch = new Float32Array(overlapSamples);
        }
        const output = this.overlapScratch;
        const input = inputBuffer.readSubarray(inputPosition, inputPosition + overlapSamples);
        const frameScale = 1 / this.overlapLength;
        for (let i = 0; i < this.overlapLength; i++) {
            const tempFrame = (this.overlapLength - i) * frameScale;
            const fi = i * frameScale;
            const ctx = 2 * i;
            const inputLeft = ctx < input.length ? input[ctx] : 0;
            const inputRight = ctx + 1 < input.length ? input[ctx + 1] : 0;
            output[ctx] = inputLeft * fi + this.midBuffer[ctx] * tempFrame;
            output[ctx + 1] = inputRight * fi + this.midBuffer[ctx + 1] * tempFrame;
        }
        outputBuffer.appendSamples(output, this.overlapLength);
    }
    process() {
        const inputBuffer = this.getInputBufferAdapter();
        const outputBuffer = this.getOutputBufferAdapter();
        if (!this.bootstrapMidBuffer(inputBuffer)) {
            return;
        }
        while (inputBuffer.frameCount >= this.sampleReq) {
            this.processOneWindow(inputBuffer, outputBuffer);
        }
    }
    bootstrapMidBuffer(inputBuffer) {
        if (!this.midBufferDirty) {
            return true;
        }
        if (inputBuffer.frameCount < this.overlapLength) {
            return false;
        }
        const needed = this.overlapLength * 2;
        if (!this.midBuffer || this.midBuffer.length < needed) {
            this.midBuffer = new Float32Array(needed);
        }
        inputBuffer.receiveSamples(this.midBuffer, this.overlapLength);
        this.midBufferDirty = false;
        return true;
    }
    processOneWindow(inputBuffer, outputBuffer) {
        const offset = this.seekBestOverlapPosition(inputBuffer);
        this.overlapStereo(2 * Math.floor(offset), inputBuffer, outputBuffer);
        const middleFrames = this.seekWindowLength - 2 * this.overlapLength;
        if (middleFrames > 0) {
            outputBuffer.putFrom(inputBuffer, offset + this.overlapLength, middleFrames);
        }
        this.captureOverlapHistory(offset, inputBuffer);
        this.advanceInputByNominalSkip(inputBuffer);
    }
    captureOverlapHistory(offset, inputBuffer) {
        const start = inputBuffer.startIndex +
            2 * (offset + this.seekWindowLength - this.overlapLength);
        this.midBuffer.set(inputBuffer.readSubarray(start, start + 2 * this.overlapLength));
    }
    advanceInputByNominalSkip(inputBuffer) {
        this.skipFract += this.nominalSkip;
        const overlapSkip = Math.floor(this.skipFract);
        this.skipFract -= overlapSkip;
        inputBuffer.receive(overlapSkip);
    }
    getInputBufferAdapter() {
        if (this._inputBuffer === null) {
            throw new Error('inputBuffer is not set');
        }
        this.inputBufferAdapter.setBuffer(this._inputBuffer);
        return this.inputBufferAdapter;
    }
    getOutputBufferAdapter() {
        if (this._outputBuffer === null) {
            throw new Error('outputBuffer is not set');
        }
        this.outputBufferAdapter.setOutputBuffer(this._outputBuffer);
        return this.outputBufferAdapter;
    }
}
//# sourceMappingURL=Stretch.js.map