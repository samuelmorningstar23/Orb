#!/usr/bin/env python3
"""Orb MedGemma 4B QLoRA Fine-Tuning Harness

Prepares high-quality clinical prompt-response pairs from MTSamples and 
Indian clinical brand-to-generic mappings, formats them using the MedGemma 1.5 
chat template, and runs QLoRA fine-tuning natively on Apple Silicon via MLX-LM.
"""
import os
import sys
import csv
import json
import random
import argparse
import subprocess
from pathlib import Path
from typing import List, Dict

# Set up paths
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

# Indian Clinical Brand to Generic & Indication mappings
INDIAN_DRUG_QA = [
    {
        "query": "What is Dolo 650 and when is it prescribed?",
        "response": "Dolo 650 is a widely used Indian brand of Paracetamol (Acetaminophen) 650 mg. It is prescribed as an antipyretic to treat high fever and as an analgesic for mild-to-moderate pain, such as headaches or joint aches. Always verify with pharmacy."
    },
    {
        "query": "Can a patient take Crocin for pain relief?",
        "response": "Yes, Crocin is a popular Indian brand of Paracetamol (Acetaminophen). It works as an analgesic for mild pain relief (headache, body aches) and as an antipyretic to reduce fever. Max dose is 4g daily to avoid hepatotoxicity."
    },
    {
        "query": "What is Monocef 1g IV used for?",
        "response": "Monocef is an Indian brand name for Ceftriaxone, a third-generation cephalosporin broad-spectrum antibiotic. The 1g IV formulation is used for severe bacterial infections, including pneumonia, meningitis, and urinary tract infections."
    },
    {
        "query": "What are the indications for Pantocid?",
        "response": "Pantocid is an Indian brand of Pantoprazole, a proton pump inhibitor (PPI). It is indicated for gastroesophageal reflux disease (GERD), peptic ulcers, and prophylaxis against drug-induced gastric irritation. Take 30-60 minutes before breakfast."
    },
    {
        "query": "What is in Augmentin 625?",
        "response": "Augmentin 625 is an Indian brand of co-amoxiclav. It contains 500 mg of Amoxicillin (a beta-lactam antibiotic) and 125 mg of Clavulanic Acid (a beta-lactamase inhibitor), making it highly effective against resistant bacterial infections."
    },
    {
        "query": "What is Combiflam used to treat?",
        "response": "Combiflam is a widely used Indian brand combining Ibuprofen (400 mg) and Paracetamol (325 mg). It is indicated for acute inflammatory conditions, joint pains, osteoarthritis, and moderate pain. Administer with food to reduce GI upset."
    },
    {
        "query": "What is Omez and its typical dosage?",
        "response": "Omez is an Indian brand of Omeprazole, a proton pump inhibitor (PPI) used for acid reflux, GERD, and hypersecretory conditions. The typical dosage is 20 mg orally once daily before the morning meal."
    },
    {
        "query": "Can you explain what Montek-LC contains?",
        "response": "Montek-LC is a combination drug containing Montelukast (10 mg), a leukotriene receptor antagonist, and Levocetirizine (5 mg), an antihistamine. It is prescribed for allergic rhinitis, asthma symptoms, and chronic hives."
    },
    {
        "query": "What is Calpol and its pediatric usage?",
        "response": "Calpol is an Indian brand of Paracetamol (Acetaminophen), highly common in pediatric suspension forms for fever reduction and mild pain. Pediatric dosage must be calculated precisely by body weight (15 mg/kg per dose)."
    },
    {
        "query": "What antibiotic class does Ceftum belong to?",
        "response": "Ceftum is an Indian brand of Cefuroxime Axetil, which belongs to the second-generation cephalosporin class of antibiotics. It is prescribed for respiratory, ear, nose, throat, skin, and urinary tract infections."
    },
    {
        "query": "What generic drug is Taxim?",
        "response": "Taxim is an Indian brand name for Cefotaxime, a third-generation cephalosporin antibiotic. It is widely used intravenously to treat severe infections like septicemia, lower respiratory infections, and pelvic inflammatory disease."
    },
    {
        "query": "What is Zifi 200 prescribed for?",
        "response": "Zifi 200 contains Cefixime (200 mg), an oral third-generation cephalosporin antibiotic. It is prescribed for uncomplicated urinary tract infections, otitis media, pharyngitis, and uncomplicated gonorrhea."
    },
    {
        "query": "What is Becosules?",
        "response": "Becosules is a widely prescribed Indian brand of Vitamin B-complex capsules with Vitamin C. It is used to treat or prevent vitamin deficiencies caused by poor diet, certain illnesses, or chronic antibiotic therapies."
    },
    {
        "query": "What does Shelcal 500 contain?",
        "response": "Shelcal 500 is a calcium and vitamin D3 supplement. It contains Calcium Carbonate (500 mg elemental calcium equivalent) and Cholecalciferol (Vitamin D3 250 IU) to promote bone health and treat osteoporosis."
    },
    {
        "query": "What is Ascoril and when is it taken?",
        "response": "Ascoril is an Indian brand of cough syrup containing a combination of Terbutaline (bronchodilator), Bromhexine (mucolytic), and Guaifenesin (expectorant). It is taken to relieve productive cough associated with bronchospasm."
    },
    {
        "query": "What is Avil and what are its side effects?",
        "response": "Avil is an Indian brand of Pheniramine Maleate, a first-generation antihistamine used to treat allergic reactions, hives, and runny nose. The primary side effects are significant drowsiness, dry mouth, and blurred vision."
    },
    {
        "query": "What is Pan-D and how does it work?",
        "response": "Pan-D is a combination of Pantoprazole (40 mg, proton pump inhibitor) and Domperidone (30 mg, prokinetic antiemetic). It works by reducing stomach acid and accelerating gastric emptying to relieve acid reflux with nausea."
    }
]


def format_gemma_prompt(system_prompt: str, user_query: str, assistant_response: str) -> str:
    """Format input into standard Gemma 1.5 chat template."""
    return (
        f"<start_of_turn>system\n{system_prompt}<end_of_turn>\n"
        f"<start_of_turn>user\n{user_query}<end_of_turn>\n"
        f"<start_of_turn>model\n{assistant_response}<end_of_turn>\n"
    )


def process_mtsamples(csv_path: Path, max_samples: int = 250) -> List[Dict[str, str]]:
    """Parse transcription reports from MTSamples CSV and create training pairs."""
    print(f"Reading MTSamples dataset from {csv_path}...")
    
    examples = []
    
    if not csv_path.exists():
        print(f"❌ MTSamples CSV not found at {csv_path}")
        return []
        
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
    # Shuffle to get a representative mix of medical specialties
    random.seed(42)
    random.shuffle(rows)
    
    count = 0
    for r in rows:
        specialty = r.get("medical_specialty", "").strip()
        transcription = r.get("transcription", "").strip()
        description = r.get("description", "").strip()
        sample_name = r.get("sample_name", "").strip()
        
        if not transcription or len(transcription) < 200:
            continue
            
        # Truncate transcription to prevent GPU execution buffer timeouts
        trans_compact = transcription[:300] + "..." if len(transcription) > 300 else transcription
            
        # Task 1: Clinical SOAP summarizing / Documentation
        system_doc = "You are Sage, a clinical documentation assistant. Summarize the following clinician dictation into a structured clinical report."
        user_doc = f"Clinical specialty: {specialty}\nReport topic: {sample_name}\n\nDictation:\n{trans_compact}"
        assistant_doc = f"Here is the structured summary:\n\nSpecialty: {specialty}\nCase Summary: {description}\n\nFull Transcription Analysis:\n{trans_compact}"
        
        examples.append({
            "text": format_gemma_prompt(system_doc, user_doc, assistant_doc)
        })
        
        # Task 2: Actionable diagnosis extraction
        system_ext = "You are Sage, an AI clinical chart auditor. Extract the clinical diagnosis and comorbidities from the following report."
        user_ext = f"Extract diagnosis and comorbidities from:\n{trans_compact}"
        assistant_ext = f"Extracted clinical analysis:\nPrimary Diagnosis: {sample_name}\nContext: {description}"
        
        examples.append({
            "text": format_gemma_prompt(system_ext, user_ext, assistant_ext)
        })
        
        count += 1
        if count >= max_samples:
            break
            
    print(f"Generated {len(examples)} training examples from MTSamples.")
    return examples


def build_finetuning_data(csv_path: Path, output_dir: Path, max_samples: int = 250):
    """Combine MTSamples data with Indian Drug QA mappings and split into train/validation sets."""
    print("Building QLoRA training dataset...")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Get processed MTSamples
    mtsamples_examples = process_mtsamples(csv_path, max_samples)
    
    # 2. Get preformatted Indian Drug QAs
    indian_examples = []
    system_qa = "You are Sage, a conversational clinical assistant. Answer the query concisely, providing brand-to-generic explanations."
    for item in INDIAN_DRUG_QA:
        indian_examples.append({
            "text": format_gemma_prompt(system_qa, item["query"], item["response"])
        })
    print(f"Generated {len(indian_examples)} Indian drug brand custom QA examples.")
    
    # Combine and shuffle
    all_examples = mtsamples_examples + indian_examples
    random.shuffle(all_examples)
    
    # Train-Validation split (90% Train, 10% Valid)
    split_idx = int(len(all_examples) * 0.9)
    train_data = all_examples[:split_idx]
    valid_data = all_examples[split_idx:]
    
    # Write to JSONL
    train_file = output_dir / "train.jsonl"
    valid_file = output_dir / "valid.jsonl"
    
    with open(train_file, "w", encoding="utf-8") as f:
        for ex in train_data:
            f.write(json.dumps(ex) + "\n")
            
    with open(valid_file, "w", encoding="utf-8") as f:
        for ex in valid_data:
            f.write(json.dumps(ex) + "\n")
            
    print(f"Saved {len(train_data)} training examples to {train_file}")
    print(f"Saved {len(valid_data)} validation examples to {valid_file}")
    print("Dataset preparation complete!")


def run_lora_training(model_path: Path, data_dir: Path, iters: int = 100, batch_size: int = 4, layers: int = 16):
    """Execute mlx_lm.lora parameter-efficient fine-tuning on Apple Silicon."""
    print("\n=========================================================")
    print("               Initiating Apple Silicon QLoRA            ")
    print("=========================================================")
    print(f"Model:      {model_path}")
    print(f"Data:       {data_dir}")
    print(f"Iterations: {iters}")
    print(f"Batch Size: {batch_size}")
    print(f"LoRA Layers:{layers}")
    print("=========================================================\n")
    
    cmd = [
        sys.executable, "-m", "mlx_lm", "lora",
        "--model", str(model_path),
        "--data", str(data_dir),
        "--train",
        "--iters", str(iters),
        "--batch-size", str(batch_size),
        "--num-layers", str(layers),
        "--max-seq-length", "256",
        "--grad-checkpoint"
    ]
    
    print(f"Running Command: {' '.join(cmd)}")
    try:
        # Stream outputs of lora fine-tuning to the user in real-time
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        
        for line in process.stdout:
            print(line, end="")
            
        process.wait()
        if process.returncode == 0:
            print("\n✅ QLoRA training successfully finished!")
            print("Adapters saved as adapters.safetensors. Ready to serve via voice_agent_service.py!")
        else:
            print(f"\n❌ QLoRA training exited with return code: {process.returncode}")
            
    except Exception as e:
        print(f"\n❌ Failed to launch QLoRA training: {e}")


def main():
    parser = argparse.ArgumentParser(description="Orb MedGemma QLoRA Fine-Tuning Harness")
    parser.add_argument("--prepare-only", action="store_true", help="Only prepare datasets, do not start training")
    parser.add_argument("--iters", type=int, default=100, help="Number of fine-tuning iterations (default: 100)")
    parser.add_argument("--batch-size", type=int, default=4, help="Batch size (default: 4)")
    parser.add_argument("--layers", type=int, default=16, help="Number of layers to freeze/tune (default: 16)")
    args = parser.parse_args()
    
    csv_path = SERVER_DIR / "datasets" / "mtsamples" / "mtsamples.csv"
    output_dir = SERVER_DIR / "datasets" / "mtsamples" / "processed"
    model_path = SERVER_DIR / "models" / "medgemma-1.5-4b-it"
    
    # 1. Build training dataset
    build_finetuning_data(csv_path, output_dir, max_samples=250)
    
    # 2. Run LoRA training if not prepare-only
    if not args.prepare_only:
        if not model_path.exists():
            print(f"❌ MedGemma 4B model not found at: {model_path}")
            print("Please download it first or double-check its path.")
            return
            
        run_lora_training(model_path, output_dir, iters=args.iters, batch_size=args.batch_size, layers=args.layers)


if __name__ == "__main__":
    main()
