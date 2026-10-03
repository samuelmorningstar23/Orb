import os
import soundfile as sf
import jsonlines
from datasets import load_dataset
from tqdm import tqdm

def process_dataset(dataset_name, split, output_dir, limit=10000, text_column=None):
    print(f"Processing {dataset_name} (split: {split})")
    
    # HF cache dir (resolved relative to this file, machine-independent)
    cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "datasets")
    
    try:
        # Load the dataset
        ds = load_dataset(dataset_name, split=split, cache_dir=cache_dir, streaming=True)
    except Exception as e:
        print(f"Error loading {dataset_name}: {e}")
        return

    # Directories for WAVs and metadata
    ds_name_safe = dataset_name.replace("/", "_")
    wavs_dir = os.path.join(output_dir, ds_name_safe, "wavs")
    os.makedirs(wavs_dir, exist_ok=True)
    jsonl_path = os.path.join(output_dir, ds_name_safe, "train.jsonl")

    # Discover text column if not provided
    if text_column is None:
        possible_text_cols = ['text', 'transcript', 'transcription', 'sentence', 'normalized_text']
        cols = list(ds.features.keys())
        for col in possible_text_cols:
            if col in cols:
                text_column = col
                break
        
        if text_column is None:
            print(f"Could not find a text column in {dataset_name}. Columns: {cols}")
            return

    audio_column = 'audio'
    if audio_column not in cols:
        print(f"Could not find an audio column in {dataset_name}. Columns: {cols}")
        return

    print(f"Using audio column: '{audio_column}', text column: '{text_column}'")

    count = 0
    with jsonlines.open(jsonl_path, mode='w') as writer:
        for i, item in enumerate(tqdm(ds, total=limit)):
            if i >= limit:
                break
            
            audio_data = item[audio_column]
            text_data = item[text_column]

            if audio_data is None:
                continue
                
            try:
                array = audio_data['array']
                sr = audio_data['sampling_rate']
            except Exception as e:
                try:
                    # In case of AudioDecoder or similar HF dataset format
                    array = audio_data['array']
                    sr = audio_data.get('sampling_rate', 16000)
                except Exception as ex:
                    print(f"Unexpected audio format at index {i}: {type(audio_data)}, error: {ex}")
                    continue

            wav_filename = f"{count:06d}.wav"
            wav_filepath = os.path.join(wavs_dir, wav_filename)
            
            # Save audio as WAV
            sf.write(wav_filepath, array, sr)
            
            # Write metadata
            # MLX whisper format expects {"audio": "path/to.wav", "text": "transcript"}
            # We can use absolute path or relative to jsonl directory. Let's use absolute as requested format
            writer.write({
                "audio": wav_filepath,
                "text": text_data
            })
            count += 1
            
    print(f"Finished processing {count} samples for {dataset_name}.")
    print(f"WAVs saved in: {wavs_dir}")
    print(f"Metadata saved in: {jsonl_path}\n")

if __name__ == "__main__":
    output_base_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "training_data", "audio")
    os.makedirs(output_base_dir, exist_ok=True)
    
    datasets_to_process = [
        {"name": "intelmedica/medical-tts-parquet-2-16khz", "split": "train"},
        {"name": "ekacare/eka-medical-asr-evaluation-dataset", "split": "test"} # 'train' split not available in eka-medical-asr-evaluation-dataset
    ]
    
    for ds_info in datasets_to_process:
        process_dataset(ds_info["name"], ds_info["split"], output_base_dir, limit=10000)
