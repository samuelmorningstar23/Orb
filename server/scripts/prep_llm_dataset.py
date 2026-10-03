import pandas as pd
import json
import os
import random

def format_conversation(transcription, specialty, description):
    """
    Format into a conversational JSONL structure expected by MLX LoRA.
    """
    system_prompt = "You are Sage, a clinical AI assistant. Extract clinical facts, assessments, and action intents from the provided clinical transcription."
    
    # We construct a synthetic target output based on the description and specialty
    # In a real pipeline, we would bootstrap this with a teacher model (e.g., GPT-4 or MedGemma 27B)
    target_json = {
        "action_intent": "none",
        "concise_answer": description.strip(),
        "display_mode": "detailed",
        "patient_facts_summary": f"Patient evaluated for {specialty.strip()}.",
        "local_policy_guidance": "",
        "external_guidance": "",
        "clinical_assessment": description.strip(),
        "caveats": "",
        "escalation": "none",
        "cited_sources": []
    }
    
    # Format the prompt using MedGemma's expected special tokens if necessary
    text = f"<system>{system_prompt}</system> <user>{transcription.strip()}</user> <model>{json.dumps(target_json)}</model>"
    return {"text": text}

def main():
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dataset_path = os.path.join(base_dir, "datasets", "mtsamples", "mtsamples.csv")
    output_dir = os.path.join(base_dir, "training_data", "llm")
    os.makedirs(output_dir, exist_ok=True)
    
    df = pd.read_csv(dataset_path)
    df = df.dropna(subset=['transcription', 'description', 'medical_specialty'])
    
    data = []
    for _, row in df.iterrows():
        data.append(format_conversation(row['transcription'], row['medical_specialty'], row['description']))
        
    random.shuffle(data)
    
    # 90/10 Split
    split_idx = int(len(data) * 0.9)
    train_data = data[:split_idx]
    valid_data = data[split_idx:]
    
    with open(os.path.join(output_dir, "train.jsonl"), "w") as f:
        for item in train_data:
            f.write(json.dumps(item) + "\n")
            
    with open(os.path.join(output_dir, "valid.jsonl"), "w") as f:
        for item in valid_data:
            f.write(json.dumps(item) + "\n")
            
    print(f"Generated {len(train_data)} training and {len(valid_data)} validation samples for MedGemma in {output_dir}")

if __name__ == "__main__":
    main()
