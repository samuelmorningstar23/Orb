#!/bin/bash
# train_medgemma.sh
# Fine-tunes MedGemma-1.5-4B-IT using mlx_lm.lora

MODEL_PATH="../models/medgemma-1.5-4b-it"
DATA_PATH="../training_data/llm"
ADAPTER_PATH="../adapters/medgemma-lora"

echo "[MLX] Starting MedGemma LoRA fine-tuning on Apple Silicon..."

python3=../.venv/bin/python3

$python3 -m mlx_lm.lora \
    --model "$MODEL_PATH" \
    --data "$DATA_PATH" \
    --train \
    --batch-size 1 \
    --val-batches 1 \
    --max-seq-length 1024 \
    --grad-checkpoint \
    --num-layers 8 \
    --iters 1000 \
    --learning-rate 1e-5 \
    --adapter-path "$ADAPTER_PATH"

echo "[MLX] Training complete. Adapters saved to $ADAPTER_PATH"
