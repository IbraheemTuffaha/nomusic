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
/**
 * Abstract base class for sample processing pipes.
 *
 * @remarks
 * Manages input and output buffers for audio sample processing chains. Subclasses should implement
 * specific processing logic for audio transformation or analysis. This class is not intended to be used
 * directly, but as a base for concrete audio processing stages.
 *
 * @typeParam TInputBuffer - Concrete input buffer type (defaults to the generic `SampleBuffer` contract).
 * @typeParam TOutputBuffer - Concrete output buffer type (defaults to `TInputBuffer` so input/output share the same buffer type unless a subclass opts into different types).
 */
export default class AbstractSamplePipe {
    /**
     * Input buffer for audio samples.
     */
    _inputBuffer;
    /**
     * Output buffer for processed audio samples.
     */
    _outputBuffer;
    /**
     * Constructs an AbstractSamplePipe.
     * @param options Constructor options.
     *
     * @remarks
     * When `createBuffers` is true, both factories are required so subclasses can
     * control exact buffer implementations without unsafe casting.
     */
    constructor({ createBuffers = false, inputBufferFactory, outputBufferFactory, } = {}) {
        if (createBuffers) {
            if (!inputBufferFactory || !outputBufferFactory) {
                throw new Error('buffer factories are required when createBuffers is true');
            }
            this._inputBuffer = inputBufferFactory();
            this._outputBuffer = outputBufferFactory();
        }
        else {
            this._inputBuffer = null;
            this._outputBuffer = null;
        }
    }
    /**
     * Gets the input buffer.
     * @returns The current input buffer instance, or null if not set.
     */
    get inputBuffer() {
        return this._inputBuffer;
    }
    /**
     * Sets the input buffer.
     * @param inputBuffer The new input buffer instance, or null to unset.
     */
    set inputBuffer(inputBuffer) {
        this._inputBuffer = inputBuffer;
    }
    /**
     * Gets the output buffer.
     * @returns The current output buffer instance, or null if not set.
     */
    get outputBuffer() {
        return this._outputBuffer;
    }
    /**
     * Sets the output buffer.
     * @param outputBuffer The new output buffer instance, or null to unset.
     */
    set outputBuffer(outputBuffer) {
        this._outputBuffer = outputBuffer;
    }
    /**
     * Clears both input and output buffers.
     *
     * @remarks
     * Resets the state of both input and output buffers, if present, by calling their `clear()` methods.
     */
    clear() {
        this._inputBuffer?.clear();
        this._outputBuffer?.clear();
    }
}
//# sourceMappingURL=AbstractSamplePipe.js.map