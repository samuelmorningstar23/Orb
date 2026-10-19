import os
import sys
import time
import gc
from pathlib import Path

# Set up paths
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))

# Questions list
BASIC_QUESTIONS = [
    "What is the standard adult dose of Paracetamol (Acetaminophen) and the maximum daily dose?",
    "What are the common side effects of Metoprolol?",
    "How does Metformin lower blood glucose levels?",
    "What is the difference between type 1 and type 2 diabetes mellitus?",
    "What are the classic symptoms of a myocardial infarction?",
    "What is the mechanism of action of Penicillin?"
]

COMPLEX_QUESTIONS = [
    "A 65-year-old male with a history of CHF and stage 3 CKD presents with acute gouty arthritis. What is the preferred acute management, and why should NSAIDs and Colchicine be avoided or used with extreme caution?",
    "A 42-year-old female presents with clinical signs of deep vein thrombosis (DVT). How do you use the Wells Criteria and D-dimer to guide diagnostic decision making?",
    "A patient on Warfarin therapy presents with an INR of 7.2 but no signs of active bleeding. What is the appropriate management strategy according to CHEST guidelines?",
    "A 55-year-old female is diagnosed with severe sepsis. What are the critical elements of the surviving sepsis campaign 1-hour bundle?",
    "A patient with COPD exacerbation is on non-invasive positive pressure ventilation (NIPPV). What clinical and arterial blood gas (ABG) criteria indicate that NIPPV is failing and intubation is required?"
]

USMLE_QUESTIONS = [
    "A 4-year-old boy is brought to the clinic because of difficulty climbing stairs and rising from the floor. Physical examination shows hypertrophy of the calves and a positive Gowers' sign. A muscle biopsy is performed. Which of the following is the most likely genetic mechanism responsible for this patient's condition?",
    "A 28-year-old woman presents to the clinic with fatigue, cold intolerance, and weight gain. Physical examination reveals a diffuse, painless goiter. Laboratory studies show elevated TSH and low free T4. Serum anti-thyroid peroxidase (anti-TPO) antibodies are highly elevated. Histological examination of the thyroid gland would most likely reveal which of the following?",
    "A 62-year-old man presents with progressive dyspnea and a nonproductive cough. He worked in shipyards for 30 years. Chest X-ray shows pleural plaques and interstitial fibrosis. A lung biopsy reveals ferruginous bodies. This patient is at the greatest risk for developing which of the following neoplasms?",
    "A 35-year-old male is brought to the emergency department after a motor vehicle collision. He is hypotensive (BP 80/50 mmHg), tachycardic (HR 120 bpm), and has distended neck veins. Tracheal deviation is not present. Breath sounds are equal bilaterally. What is the most likely diagnosis, and what is the immediate management?",
    "A 45-year-old woman presents with persistent headache and visual changes. Brain MRI shows a pituitary macroadenoma. Laboratory evaluation reveals elevated prolactin levels. She also reports a family history of kidney stones and recurrent peptic ulcer disease. Which of the following genetic syndromes is most likely in this patient, and what other endocrine organs are typically involved?"
]

def main():
    model_path = str(SERVER_DIR / "models" / "medgemma-1.5-4b-it")
    print(f"Loading model from {model_path}...")
    
    # Try using mlx_lm
    try:
        from mlx_lm import load, generate
        import mlx.core as mx
        print("Using mlx_lm for inference.")
        
        from mlx_lm.sample_utils import make_sampler
        model, tokenizer = load(model_path)
        
        def ask(prompt):
            formatted_prompt = f"<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
            response = generate(
                model,
                tokenizer,
                prompt=formatted_prompt,
                max_tokens=256,
                sampler=make_sampler(temp=0.3),
                verbose=False
            )
            return response
            
    except ImportError:
        print("mlx_lm not available, trying transformers (CPU/MPS)...")
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        
        device = "mps" if torch.backends.mps.is_available() else "cpu"
        print(f"Using device: {device}")
        
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(model_path, local_files_only=True, torch_dtype=torch.float16 if device == "mps" else torch.float32)
        model.to(device)
        
        def ask(prompt):
            messages = [{"role": "user", "content": prompt}]
            text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = tokenizer([text], return_tensors="pt").to(device)
            
            with torch.no_grad():
                generated_ids = model.generate(
                    **inputs,
                    max_new_tokens=256,
                    temperature=0.3,
                    do_sample=True
                )
            # trim input
            generated_ids = [
                output_ids[len(input_ids):] for input_ids, output_ids in zip(inputs.input_ids, generated_ids)
            ]
            response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
            return response

    print("\n--- STARTING TESTS ---\n")
    
    # 6 Basic Questions
    print("=== 6 BASIC QUESTIONS ===")
    for i, q in enumerate(BASIC_QUESTIONS, 1):
        print(f"\nQ{i}: {q}")
        t_start = time.time()
        answer = ask(q)
        t_end = time.time()
        print(f"A{i} ({t_end - t_start:.2f}s): {answer.strip()}")
        print("-" * 40)
        
    # 5 Complex Questions
    print("\n=== 5 COMPLEX QUESTIONS ===")
    for i, q in enumerate(COMPLEX_QUESTIONS, 1):
        print(f"\nQ{i}: {q}")
        t_start = time.time()
        answer = ask(q)
        t_end = time.time()
        print(f"A{i} ({t_end - t_start:.2f}s): {answer.strip()}")
        print("-" * 40)

    # 5 USMLE Questions
    print("\n=== 5 USMLE QUESTIONS ===")
    for i, q in enumerate(USMLE_QUESTIONS, 1):
        print(f"\nQ{i}: {q}")
        t_start = time.time()
        answer = ask(q)
        t_end = time.time()
        print(f"A{i} ({t_end - t_start:.2f}s): {answer.strip()}")
        print("-" * 40)

    print("\n=== TESTING COMPLETE. UNLOADING MODEL ===\n")
    
    # Clean up model references and run garbage collection to free memory
    # Clear the closure cell without deleting its binding (keeps static checks valid).
    model = None
    if 'tokenizer' in locals():
        tokenizer = None
    
    gc.collect()
    
    # If using mlx, clear cache
    try:
        import mlx.core as mx
        mx.metal.clear_cache()
        print("MLX Metal cache cleared.")
    except:
        pass
        
    # If using torch, clear cache
    try:
        import torch
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
            print("MPS cache cleared.")
    except:
        pass
        
    print("Model unloaded successfully.")

if __name__ == "__main__":
    main()
