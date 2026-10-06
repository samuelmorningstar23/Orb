"""Where the on-device speech model lives, and whether it is there.

Orb is strictly offline (owner ruling, 4 October 2026). The Whisper weights are
installed with the appliance under server/mlx_models/<name>/ and nothing ever
downloads them at runtime. lightning_whisper_mlx's own LightningWhisperMLX
constructor exists to fetch them from Hugging Face, so the warm daemon and the
subprocess worker no longer call it: they check the folder here and hand its
path straight to transcribe_audio, which loads a local path without the hub.

Stdlib only, because the subprocess worker imports it and is kept free of MLX
and of the server's own modules until it has to transcribe.
"""
import os
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
MLX_MODELS_DIR = SERVER_DIR / "mlx_models"
#: The two files lightning_whisper_mlx's load_model reads from the folder.
REQUIRED_FILES = ("config.json", "weights.npz")


class SpeechModelNotInstalled(RuntimeError):
    """The Whisper weights are not on this appliance."""


def force_hub_offline() -> None:
    """Make any Hugging Face lookup in this process local-only. Call it before
    huggingface_hub is imported: the library reads HF_HUB_OFFLINE once, at
    import time."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


def model_dir_name(model: str, quant: str | None = None) -> str:
    """The folder name lightning_whisper_mlx gives a model: the model name, plus
    -4-bit or -8-bit for a quantised distil model."""
    if quant and "distil" in model:
        return model + ("-4-bit" if quant == "4bit" else "-8-bit")
    return model


def model_dir(model: str, quant: str | None = None, models_dir: Path | None = None) -> Path:
    return Path(models_dir or MLX_MODELS_DIR) / model_dir_name(model, quant)


def missing_files(model: str, quant: str | None = None, models_dir: Path | None = None) -> list[str]:
    folder = model_dir(model, quant, models_dir)
    return [name for name in REQUIRED_FILES if not (folder / name).is_file()]


def is_installed(model: str, quant: str | None = None, models_dir: Path | None = None) -> bool:
    return not missing_files(model, quant, models_dir)


def require_installed(model: str, quant: str | None = None, models_dir: Path | None = None) -> Path:
    """The local model folder, or SpeechModelNotInstalled naming what is missing."""
    folder = model_dir(model, quant, models_dir)
    missing = missing_files(model, quant, models_dir)
    if missing:
        raise SpeechModelNotInstalled(
            f"speech model not installed: {folder} has no {', '.join(missing)}. "
            "Orb is offline and never downloads it; the Whisper weights have to be "
            "installed with the appliance"
        )
    return folder
