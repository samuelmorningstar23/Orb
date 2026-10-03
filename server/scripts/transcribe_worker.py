#!/usr/bin/env python3
"""Standalone Whisper transcription worker.
Run as subprocess to isolate MLX/tiktoken from main server process.
Usage: python transcribe_worker.py <audio_path> [model_name] [hints] [batch_size] [quant]
Prints JSON result to stdout.
"""
import sys, json, os, subprocess, tempfile


def convert_to_wav(input_path: str) -> str:
    """Convert audio to 16kHz mono WAV using ffmpeg for best Whisper accuracy."""
    wav_path = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-i", input_path,
                "-ar", "16000",      # 16kHz sample rate (Whisper native)
                "-ac", "1",          # mono
                "-c:a", "pcm_s16le", # 16-bit PCM
                wav_path,
            ],
            capture_output=True, timeout=30,
        )
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 100:
            return wav_path
    except Exception:
        pass
    return input_path  # fallback to original if conversion fails


def main():
    audio_path = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else "large-v3"
    hints = sys.argv[3] if len(sys.argv) > 3 else ""
    batch_size = int(sys.argv[4]) if len(sys.argv) > 4 else 12
    quant = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None
    language = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else "en"

    # Convert webm/other formats to WAV for best accuracy
    converted_path = convert_to_wav(audio_path)

    from lightning_whisper_mlx import LightningWhisperMLX
    from lightning_whisper_mlx.transcribe import transcribe_audio

    # Initialize with configurable params
    whisper = LightningWhisperMLX(model=model, batch_size=batch_size, quant=quant)
    
    # Generic medical context prompt to bias the decoder towards clinical terminology
    clinical_prompt = "Medical dictation: Patient admitted to ward, bed, IV. Bed 1. Bed 2. Bed 3. Patient case, diagnosis, vitals, ICD. Fosfomycin, Meropenem, Vancomycin, Cefepime, Piperacillin/Tazobactam. "
    if hints:
        clinical_prompt += f"Specific names and context: {hints}"

    # 'auto' → let Whisper detect the language (pass None) for code-switching.
    whisper_language = None if language == "auto" else language
    result = transcribe_audio(
        converted_path,
        path_or_hf_repo=f'./mlx_models/{whisper.name}',
        language=whisper_language,
        batch_size=batch_size,
        initial_prompt=clinical_prompt
    )
    
    result_json = json.dumps({"text": result["text"]})
    print(f"###RESULT###{result_json}###RESULT###")

    # Clean up converted file
    if converted_path != audio_path:
        try:
            os.remove(converted_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()
