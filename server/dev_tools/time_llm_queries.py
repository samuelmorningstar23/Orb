import requests
import json
import time
import sys

BASE = "http://localhost:8000"

def get_token():
    try:
        r = requests.post(f"{BASE}/api/auth/login", json={"employee_id": "DOC001", "password": "password"})
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:
        print(f"Error logging in: {e}")
        sys.exit(1)

def time_vigil_assessment(token, pid="PAT021"):
    print(f"\n--- Timing Vigil Assessment for Patient {pid} ---")
    headers = {"Authorization": f"Bearer {token}"}
    start_time = time.time()
    first_token_time = None
    chunks = 0
    try:
        with requests.get(f"{BASE}/api/vigil/patients/{pid}/analysis", headers=headers, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line:
                    if first_token_time is None:
                        first_token_time = time.time()
                    chunks += 1
        end_time = time.time()
        
        ttft = (first_token_time - start_time) if first_token_time else 0
        total_time = (end_time - start_time)
        print(f"Time to First Token (TTFT): {ttft:.2f} seconds")
        print(f"Total Time Taken: {total_time:.2f} seconds")
        print(f"Total Chunks Received: {chunks}")
    except Exception as e:
        print(f"Error: {e}")

def time_sage_query(token, prompt, context_pid=None):
    title = f"Sage Patient-Related Query ({context_pid})" if context_pid else "Sage Complex General Question"
    print(f"\n--- Timing {title} ---")
    headers = {"Authorization": f"Bearer {token}"}
    start_time = time.time()
    first_token_time = None
    chunks = 0
    try:
        with requests.post(f"{BASE}/api/sage/query", headers=headers, json={"query": prompt, "patient_id": context_pid}, stream=True, timeout=300) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if line:
                    if first_token_time is None:
                        first_token_time = time.time()
                    chunks += 1
        end_time = time.time()
        
        ttft = (first_token_time - start_time) if first_token_time else 0
        total_time = (end_time - start_time)
        print(f"Time to First Token (TTFT): {ttft:.2f} seconds")
        print(f"Total Time Taken: {total_time:.2f} seconds")
        print(f"Total Chunks Received: {chunks}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    token = get_token()
    # 1. Time Vigil Assessment for a patient
    time_vigil_assessment(token, pid="PAT021")
    
    # 2. Time a complex general question in Sage
    time_sage_query(token, "Explain the pharmacological mechanism of ACE inhibitors and their contraindications in bilateral renal artery stenosis.")
    
    # 3. Time a Patient-related query in Sage
    time_sage_query(token, "Based on Raj Patel's recent vitals and lab results, what are the primary clinical concerns?", context_pid="PAT021")
