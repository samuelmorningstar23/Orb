"""Orb Forecast — Training Pipeline

Generates Indian-calibrated synthetic clinical data and trains the multi-task LSTM.

Data calibration sources:
  - Indian disease prevalence (ICMR National Disease Burden, Lancet 2017)
  - Indian hospital LoS statistics (AIIMS Delhi avg ICU 5.8d, General 3.2d)
  - Indian demographic distributions (Census 2011, NFHS-5)
  - Vital sign ranges from Harrison's adapted for Indian populations

Outputs:
  - server/models/forecast_checkpoint.pt  (trained weights)
  - Populates admission_history table (last 30 days)

Usage:
  cd server && .venv/bin/python3 scripts/train_forecast.py
"""

import sys
import os
import random
import json
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import Dataset, DataLoader

from models.forecast_lstm import OrbForecastLSTM, INPUT_DIM, HIDDEN_DIM, NUM_LAYERS

# ══════════════════════════════════════════════════════════════════════
# INDIAN CLINICAL DATA PROFILES
# Calibrated to published Indian hospital statistics
# ══════════════════════════════════════════════════════════════════════

# Common Indian diagnoses with LoS distributions (days)
# Source: Multiple Indian hospital studies (AIIMS, CMC Vellore, JIPMER)
INDIAN_DIAGNOSES = {
    "critical": [
        {"diag": "Acute MI: STEMI", "los_mu": 2.0, "los_sigma": 0.4, "base_hr": 118, "base_sbp": 85},
        {"diag": "Acute stroke: MCA territory", "los_mu": 2.1, "los_sigma": 0.5, "base_hr": 95, "base_sbp": 165},
        {"diag": "Sepsis: multi-organ", "los_mu": 2.2, "los_sigma": 0.5, "base_hr": 125, "base_sbp": 78},
        {"diag": "Severe pre-eclampsia", "los_mu": 1.6, "los_sigma": 0.3, "base_hr": 105, "base_sbp": 170},
        {"diag": "Pulmonary embolism", "los_mu": 1.9, "los_sigma": 0.4, "base_hr": 115, "base_sbp": 90},
        {"diag": "Bacterial meningitis", "los_mu": 2.3, "los_sigma": 0.5, "base_hr": 110, "base_sbp": 88},
        {"diag": "Thyroid storm", "los_mu": 1.7, "los_sigma": 0.3, "base_hr": 140, "base_sbp": 155},
        {"diag": "Unstable angina", "los_mu": 1.8, "los_sigma": 0.4, "base_hr": 100, "base_sbp": 95},
        {"diag": "DKA with complications", "los_mu": 1.9, "los_sigma": 0.4, "base_hr": 112, "base_sbp": 92},
        {"diag": "Acute respiratory failure", "los_mu": 2.3, "los_sigma": 0.6, "base_hr": 120, "base_sbp": 80},
    ],
    "elevated": [
        {"diag": "Community-acquired pneumonia", "los_mu": 1.4, "los_sigma": 0.3, "base_hr": 98, "base_sbp": 108},
        {"diag": "Diabetic ketoacidosis", "los_mu": 1.3, "los_sigma": 0.3, "base_hr": 102, "base_sbp": 105},
        {"diag": "COPD exacerbation", "los_mu": 1.4, "los_sigma": 0.4, "base_hr": 95, "base_sbp": 110},
        {"diag": "Congestive heart failure", "los_mu": 1.5, "los_sigma": 0.4, "base_hr": 92, "base_sbp": 100},
        {"diag": "Dengue fever: warning signs", "los_mu": 1.2, "los_sigma": 0.3, "base_hr": 90, "base_sbp": 95},
        {"diag": "Acute pancreatitis", "los_mu": 1.5, "los_sigma": 0.4, "base_hr": 96, "base_sbp": 108},
        {"diag": "Lower GI bleed", "los_mu": 1.3, "los_sigma": 0.3, "base_hr": 100, "base_sbp": 98},
        {"diag": "Acute kidney injury", "los_mu": 1.4, "los_sigma": 0.4, "base_hr": 88, "base_sbp": 105},
        {"diag": "SLE flare: nephritis", "los_mu": 1.6, "los_sigma": 0.4, "base_hr": 94, "base_sbp": 125},
        {"diag": "Aspiration pneumonia", "los_mu": 1.5, "los_sigma": 0.4, "base_hr": 96, "base_sbp": 102},
        {"diag": "Liver cirrhosis decompensation", "los_mu": 1.7, "los_sigma": 0.5, "base_hr": 90, "base_sbp": 95},
        {"diag": "Malaria: P. falciparum", "los_mu": 1.2, "los_sigma": 0.3, "base_hr": 100, "base_sbp": 100},
        {"diag": "Tuberculosis: pulmonary", "los_mu": 1.6, "los_sigma": 0.5, "base_hr": 95, "base_sbp": 108},
    ],
    "stable": [
        {"diag": "Cholecystitis: post lap chole", "los_mu": 0.7, "los_sigma": 0.2, "base_hr": 78, "base_sbp": 122},
        {"diag": "Hyperemesis gravidarum", "los_mu": 0.6, "los_sigma": 0.2, "base_hr": 85, "base_sbp": 115},
        {"diag": "Acute pyelonephritis", "los_mu": 0.8, "los_sigma": 0.2, "base_hr": 88, "base_sbp": 118},
        {"diag": "Hip fracture: NOF", "los_mu": 1.0, "los_sigma": 0.3, "base_hr": 82, "base_sbp": 130},
        {"diag": "Cellulitis lower limb", "los_mu": 0.8, "los_sigma": 0.2, "base_hr": 80, "base_sbp": 120},
        {"diag": "Acute asthma exacerbation", "los_mu": 0.6, "los_sigma": 0.2, "base_hr": 90, "base_sbp": 125},
        {"diag": "Atrial fibrillation: new", "los_mu": 0.7, "los_sigma": 0.2, "base_hr": 95, "base_sbp": 128},
        {"diag": "Acute diverticulitis", "los_mu": 0.7, "los_sigma": 0.2, "base_hr": 82, "base_sbp": 120},
        {"diag": "Post appendectomy", "los_mu": 0.5, "los_sigma": 0.2, "base_hr": 76, "base_sbp": 118},
        {"diag": "PID", "los_mu": 0.6, "los_sigma": 0.2, "base_hr": 84, "base_sbp": 115},
        {"diag": "Viral gastroenteritis", "los_mu": 0.5, "los_sigma": 0.1, "base_hr": 80, "base_sbp": 118},
        {"diag": "Urinary tract infection", "los_mu": 0.6, "los_sigma": 0.2, "base_hr": 82, "base_sbp": 120},
        {"diag": "Alcoholic hepatitis", "los_mu": 0.9, "los_sigma": 0.3, "base_hr": 86, "base_sbp": 115},
    ],
}

# Indian name pools (Census-representative)
INDIAN_MALE_NAMES = [
    "Rajesh Kumar", "Suresh Pillai", "Manoj Tiwari", "Vikram Singh", "Arun Nambiar",
    "Harish Bhat", "Sanjay Kulkarni", "Prakash Joshi", "Ganesh Mukherjee", "Ramesh Iyengar",
    "Amit Sharma", "Deepak Verma", "Sunil Patil", "Ravi Shankar", "Mohan Das",
    "Venkat Raman", "Ashok Reddy", "Balaji Srinivasan", "Jagdish Prasad", "Kishore Nair",
    "Dinesh Gupta", "Rajiv Menon", "Santosh Kumar", "Anil Kapoor", "Girish Hegde",
    "Mahesh Babu", "Prabhu Dev", "Rakesh Roshan", "Satish Kaushik", "Umesh Yadav",
]

INDIAN_FEMALE_NAMES = [
    "Priya Reddy", "Lakshmi Devi", "Sunita Rao", "Deepa Krishnan", "Fatima Begum",
    "Shalini Gupta", "Meenakshi Sundaram", "Divya Menon", "Gayathri Venkat", "Padma Lakshmi",
    "Revathi Subramaniam", "Savitha Hegde", "Preethi Das", "Usha Rani", "Nandini Shetty",
    "Kavitha Iyer", "Anjali Sharma", "Bhavani Devi", "Chitra Ramesh", "Dhanalakshmi P",
    "Geetha Rajan", "Hema Malini", "Indira Bai", "Jyothi Kumari", "Kamala Hassan",
    "Lalitha Rao", "Malathi Krishnan", "Nalini Suresh", "Parvathi Amma", "Radha Kumari",
]

# Indian age distribution (weighted toward elderly for hospital admissions)
# Based on: Indian hospital admission age distributions from NITI Aayog health data
def sample_indian_age() -> int:
    """Sample age from Indian hospital admission distribution."""
    r = random.random()
    if r < 0.05:     return random.randint(18, 25)   # Young adults
    elif r < 0.15:   return random.randint(26, 35)   # Adults
    elif r < 0.30:   return random.randint(36, 45)   # Middle-aged
    elif r < 0.50:   return random.randint(46, 55)   # Late middle-aged
    elif r < 0.72:   return random.randint(56, 65)   # Senior
    elif r < 0.90:   return random.randint(66, 75)   # Elderly
    else:            return random.randint(76, 90)   # Very elderly


# Ward assignment probabilities by severity
WARD_MAP = {
    "critical": ["ICU"] * 8 + ["W1"] * 2,
    "elevated": ["W1"] * 3 + ["W2"] * 3 + ["W3"] * 4,
    "stable":   ["W1"] * 2 + ["W2"] * 4 + ["W3"] * 4,
}

# Day-of-week admission multipliers (Indian hospitals — higher on Mon/Tue)
DOW_MULTIPLIERS = {
    0: 1.35,  # Monday (post-weekend surge)
    1: 1.25,  # Tuesday
    2: 1.10,  # Wednesday
    3: 1.00,  # Thursday
    4: 0.95,  # Friday
    5: 0.70,  # Saturday
    6: 0.65,  # Sunday
}

# ══════════════════════════════════════════════════════════════════════
# SYNTHETIC DATASET GENERATION
# ══════════════════════════════════════════════════════════════════════

class SyntheticPatientTrajectory:
    """Generate a single patient's multivariate time-series + labels."""

    def __init__(self, seq_len: int = 48):  # 48 timesteps = ~4 days at 2-hour intervals
        self.seq_len = seq_len

        # Choose severity
        r = random.random()
        if r < 0.25:
            self.severity = "critical"
        elif r < 0.55:
            self.severity = "elevated"
        else:
            self.severity = "stable"

        # Choose diagnosis profile
        profile = random.choice(INDIAN_DIAGNOSES[self.severity])
        self.diagnosis = profile["diag"]

        # Generate true LoS (log-normal, Indian calibrated)
        self.true_los = max(1.0, np.random.lognormal(profile["los_mu"], profile["los_sigma"]))

        # Demographics
        self.sex = random.choice(["M", "F"])
        self.age = sample_indian_age()
        self.ward = random.choice(WARD_MAP[self.severity])
        self.med_count = {"critical": random.randint(6, 12),
                         "elevated": random.randint(3, 8),
                         "stable": random.randint(1, 5)}[self.severity]

        # Base vitals from diagnosis profile
        self.base_hr = profile["base_hr"]
        self.base_sbp = profile["base_sbp"]

    def generate_sequence(self, current_day: float = None) -> tuple:
        """
        Generate (features_tensor, labels_dict) for training.

        current_day: How far into the stay we're observing.
                    If None, randomly sampled.
        """
        if current_day is None:
            # Random observation point within the stay
            current_day = random.uniform(0.5, max(1.0, self.true_los - 0.5))

        remaining_los = max(0.1, self.true_los - current_day)

        # Generate vital sign time series
        features = np.zeros((self.seq_len, INPUT_DIM))

        for t in range(self.seq_len):
            time_frac = t / self.seq_len
            day_progress = current_day - (1.0 - time_frac) * 2.0  # Look-back window

            # Vital sign trajectory (with realistic drift + noise)
            improvement_factor = min(1.0, day_progress / self.true_los) if self.severity != "critical" else 0.0

            # Heart Rate
            hr_target = self.base_hr + (78 - self.base_hr) * improvement_factor * 0.5
            features[t, 0] = hr_target + np.random.normal(0, 4.0)

            # Systolic BP
            sbp_target = self.base_sbp + (120 - self.base_sbp) * improvement_factor * 0.4
            features[t, 1] = sbp_target + np.random.normal(0, 5.0)

            # Diastolic BP (correlated with SBP)
            features[t, 2] = features[t, 1] * 0.6 + np.random.normal(0, 3.0)

            # SpO2
            spo2_base = {"critical": 90.0, "elevated": 93.5, "stable": 97.0}[self.severity]
            spo2_target = spo2_base + (98 - spo2_base) * improvement_factor * 0.3
            features[t, 3] = min(100, spo2_target + np.random.normal(0, 1.0))

            # Temperature
            temp_base = {"critical": 38.8, "elevated": 37.9, "stable": 36.8}[self.severity]
            temp_target = temp_base + (36.8 - temp_base) * improvement_factor * 0.5
            features[t, 4] = temp_target + np.random.normal(0, 0.15)

            # Respiratory Rate
            rr_base = {"critical": 26, "elevated": 22, "stable": 16}[self.severity]
            rr_target = rr_base + (16 - rr_base) * improvement_factor * 0.4
            features[t, 5] = max(8, rr_target + np.random.normal(0, 1.5))

            # Static features (repeated per timestep)
            features[t, 6] = self.age
            features[t, 7] = 1.0 if self.sex == "M" else 0.0  # Sex_M
            features[t, 8] = 1.0 if self.sex == "F" else 0.0  # Sex_F
            features[t, 9]  = 1.0 if self.ward == "ICU" else 0.0
            features[t, 10] = 1.0 if self.ward == "W1" else 0.0
            features[t, 11] = 1.0 if self.ward == "W2" else 0.0
            features[t, 12] = 1.0 if self.ward == "W3" else 0.0
            features[t, 13] = 1.0 if self.severity == "critical" else 0.0
            features[t, 14] = 1.0 if self.severity == "elevated" else 0.0
            features[t, 15] = 1.0 if self.severity == "stable" else 0.0
            features[t, 16] = current_day
            features[t, 17] = self.med_count
            features[t, 18] = {"critical": 0.78, "elevated": 0.52, "stable": 0.25}[self.severity]

            # Add crisis spikes for critical patients
            if self.severity == "critical" and random.random() < 0.05:
                features[t, 0] += random.uniform(10, 25)  # HR spike
                features[t, 3] -= random.uniform(2, 6)     # SpO2 drop
                features[t, 1] -= random.uniform(10, 20)   # SBP drop

        # Labels
        # NOTE (data-quality, deferred): this whole generator is a SMOKE TEST, not
        # a validated training corpus. Vitals and every label are deterministic
        # functions of the same severity/diagnosis/improvement_factor, so the net
        # can only recover the generator's algebra. In particular the discharge
        # head must NOT be the exact analytic inverse of the LoS head
        # (1/(remaining_los+0.5)) or it just regresses a transform of the LoS label
        # with zero independent signal. Real fix: train on MIMIC-IV/eICU with an
        # actual "discharged within 24h" event + competing risks. As an interim
        # smoke-test signal, model discharge as a Bernoulli-style target with its
        # own noise and a competing-risk (deterioration blocks discharge) term so
        # the two heads are not algebraically identical.
        base_discharge = 1.0 / (remaining_los + 0.5)
        # Deterioration competing risk: critical/worsening patients are NOT
        # discharge-ready even with low remaining LoS.
        if self.severity == "critical" and improvement_factor < 0.2:
            base_discharge *= 0.3
        discharge_prob = float(np.clip(
            base_discharge + np.random.normal(0, 0.08), 0.0, 0.95))

        # Acuity trajectory
        if improvement_factor > 0.5:
            acuity = 0  # improving
        elif self.severity == "critical" and improvement_factor < 0.2:
            acuity = 2  # worsening
        else:
            acuity = 1  # stable

        labels = {
            "los": remaining_los,
            "discharge_prob": discharge_prob,
            "acuity": acuity,
        }

        return torch.tensor(features, dtype=torch.float32), labels


class ClinicalDataset(Dataset):
    """PyTorch Dataset of synthetic Indian clinical trajectories."""

    def __init__(self, n_patients: int = 500, seq_len: int = 48, augment: bool = True):
        self.samples = []

        for _ in range(n_patients):
            patient = SyntheticPatientTrajectory(seq_len)

            # Data augmentation: multiple observation points per patient
            n_obs = 3 if augment else 1
            for _ in range(n_obs):
                features, labels = patient.generate_sequence()
                self.samples.append((features, labels))

        print(f"  [Dataset] Generated {len(self.samples)} training samples from {n_patients} synthetic patients")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        features, labels = self.samples[idx]
        return (
            features,
            torch.tensor([labels["los"]], dtype=torch.float32),
            torch.tensor([labels["discharge_prob"]], dtype=torch.float32),
            torch.tensor(labels["acuity"], dtype=torch.long),
        )


# ══════════════════════════════════════════════════════════════════════
# TRAINING LOOP
# ══════════════════════════════════════════════════════════════════════

def train_model(epochs: int = 50, lr: float = 1e-3, batch_size: int = 32):
    """Train the multi-task LSTM and save checkpoint."""
    print("\n╔══════════════════════════════════════════════╗")
    print("║     Orb Forecast — LSTM Training Pipeline    ║")
    print("╚══════════════════════════════════════════════╝\n")

    # Dataset
    print("→ Generating Indian-calibrated synthetic training data...")
    dataset = ClinicalDataset(n_patients=500, seq_len=48)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True)

    # Validation set
    val_dataset = ClinicalDataset(n_patients=100, seq_len=48, augment=False)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    # Model
    model = OrbForecastLSTM()
    total_params = sum(p.numel() for p in model.parameters())
    print(f"→ Model parameters: {total_params:,}")

    # Loss functions (multi-task weighted)
    los_criterion = nn.MSELoss()
    discharge_criterion = nn.BCELoss()
    acuity_criterion = nn.CrossEntropyLoss()

    # Task loss weights
    alpha = 1.0   # LoS regression
    beta = 0.5    # Discharge probability
    gamma = 0.3   # Acuity trajectory

    # Optimizer
    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=epochs)

    best_val_loss = float("inf")
    checkpoint_path = Path(__file__).parent.parent / "models" / "forecast_checkpoint.pt"

    print(f"→ Training for {epochs} epochs (batch_size={batch_size}, lr={lr})")
    print(f"  Loss weights: LoS={alpha}, Discharge={beta}, Acuity={gamma}\n")

    for epoch in range(epochs):
        model.train()
        total_loss = 0
        los_total = 0
        dis_total = 0
        acu_total = 0

        for features, los_target, dis_target, acu_target in loader:
            optimizer.zero_grad()

            out = model(features)

            loss_los = los_criterion(out["los"], los_target)
            loss_dis = discharge_criterion(out["discharge_prob"], dis_target)
            loss_acu = acuity_criterion(out["acuity_logits"], acu_target)

            loss = alpha * loss_los + beta * loss_dis + gamma * loss_acu
            loss.backward()

            torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()

            total_loss += loss.item()
            los_total += loss_los.item()
            dis_total += loss_dis.item()
            acu_total += loss_acu.item()

        scheduler.step()

        # Validation
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for features, los_target, dis_target, acu_target in val_loader:
                out = model(features)
                v_los = los_criterion(out["los"], los_target)
                v_dis = discharge_criterion(out["discharge_prob"], dis_target)
                v_acu = acuity_criterion(out["acuity_logits"], acu_target)
                val_loss += (alpha * v_los + beta * v_dis + gamma * v_acu).item()

        avg_train = total_loss / len(loader)
        avg_val = val_loss / len(val_loader)

        if (epoch + 1) % 5 == 0 or epoch == 0:
            print(f"  Epoch {epoch+1:3d}/{epochs} │ Train: {avg_train:.4f} "
                  f"(LoS:{los_total/len(loader):.3f} Dis:{dis_total/len(loader):.3f} "
                  f"Acu:{acu_total/len(loader):.3f}) │ Val: {avg_val:.4f}")

        # Save best
        if avg_val < best_val_loss:
            best_val_loss = avg_val
            torch.save({
                "model_state_dict": model.state_dict(),
                "epoch": epoch + 1,
                "best_loss": best_val_loss,
                "input_dim": INPUT_DIM,
                "hidden_dim": HIDDEN_DIM,
                "num_layers": NUM_LAYERS,
                "training_date": datetime.now(timezone.utc).isoformat(),
                "data_source": "indian_synthetic_clinical_v1",
            }, str(checkpoint_path))

    print(f"\n✓ Training complete. Best val loss: {best_val_loss:.4f}")
    print(f"✓ Checkpoint saved: {checkpoint_path}")
    print(f"  Size: {checkpoint_path.stat().st_size / 1024:.1f} KB")

    return model


# ══════════════════════════════════════════════════════════════════════
# 30-DAY ADMISSION HISTORY GENERATOR
# ══════════════════════════════════════════════════════════════════════

def generate_admission_history():
    """
    Generate 30 days of realistic Indian hospital admission history.
    
    Calibrated to:
      - Base rate: ~4-6 admissions/day (small Indian hospital, 30-bed capacity)  
      - Day-of-week seasonality (Mon/Tue surge, weekend dip)
      - Severity distribution: 25% critical, 30% elevated, 45% stable
      - LoS: LogNormal distributions matching Indian hospital data
    """
    print("\n→ Generating 30-day admission history (Indian calibrated)...")

    # orb.db is opened through the app's helper because it may be SQLCipher-
    # encrypted at rest — a stdlib sqlite3 connection fails outright with "file is
    # not a database". The helper applies PRAGMA key first and falls back to plain
    # sqlite3 when no key is configured, and it honours the configured DB path
    # instead of assuming server/orb.db.
    from database import _open_raw_connection
    conn = _open_raw_connection()
    cursor = conn.cursor()

    # Ensure table exists
    cursor.execute("""CREATE TABLE IF NOT EXISTS admission_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admission_date DATE NOT NULL,
        patient_name TEXT NOT NULL,
        age INTEGER,
        sex TEXT,
        ward TEXT,
        diagnosis TEXT,
        severity TEXT,
        length_of_stay_days REAL,
        discharged INTEGER DEFAULT 0,
        discharge_date DATE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")

    # Clear existing history
    cursor.execute("DELETE FROM admission_history")

    now = datetime.now(timezone.utc)
    total_records = 0

    for day_offset in range(30, 0, -1):
        date = now - timedelta(days=day_offset)
        dow = date.weekday()

        # Poisson-sampled admissions with day-of-week multiplier
        base_rate = 4.5  # Average daily admissions
        adjusted_rate = base_rate * DOW_MULTIPLIERS[dow]
        n_admissions = max(1, np.random.poisson(adjusted_rate))

        for _ in range(n_admissions):
            # Severity
            r = random.random()
            if r < 0.25:
                severity = "critical"
            elif r < 0.55:
                severity = "elevated"
            else:
                severity = "stable"

            # Diagnosis
            profile = random.choice(INDIAN_DIAGNOSES[severity])

            # LoS
            los = max(1.0, round(np.random.lognormal(profile["los_mu"], profile["los_sigma"]), 1))

            # Demographics  
            sex = random.choice(["M", "F"])
            age = sample_indian_age()
            name = random.choice(INDIAN_MALE_NAMES if sex == "M" else INDIAN_FEMALE_NAMES)
            ward = random.choice(WARD_MAP[severity])

            # Determine if discharged (discharged if admission + LoS < today)
            discharge_date = date + timedelta(days=los)
            discharged = 1 if discharge_date < now else 0

            # Add some variation to admission time
            admission_hour = random.choice([8, 9, 10, 11, 14, 15, 16, 20, 21, 22])
            admission_dt = date.replace(hour=admission_hour, minute=random.randint(0, 59))

            cursor.execute(
                """INSERT INTO admission_history 
                   (admission_date, patient_name, age, sex, ward, diagnosis, severity, 
                    length_of_stay_days, discharged, discharge_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    admission_dt.strftime("%Y-%m-%d"),
                    name, age, sex, ward,
                    profile["diag"], severity, los,
                    discharged,
                    discharge_date.strftime("%Y-%m-%d") if discharged else None,
                ),
            )
            total_records += 1

    conn.commit()
    conn.close()

    print(f"✓ Generated {total_records} admission records over 30 days")
    print(f"  Avg: {total_records/30:.1f} admissions/day")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  ORB FORECAST — Model Training & Data Generation")
    print("=" * 60)

    # Step 1: Train the LSTM
    model = train_model(epochs=50)

    # Step 2: Quick inference test
    print("\n→ Running inference test...")
    test_input = torch.randn(1, 48, INPUT_DIM)
    result = model.predict(test_input)
    print(f"  Test prediction: LoS={result['predicted_los_days']}d, "
          f"P(discharge)={result['discharge_probability']:.1%}, "
          f"Trajectory={result['acuity_trajectory']} "
          f"(conf={result['confidence']:.1%})")

    # Step 3: Generate 30-day admission history
    generate_admission_history()

    print("\n✓ All done! Model and data ready for Orb Forecast.")
