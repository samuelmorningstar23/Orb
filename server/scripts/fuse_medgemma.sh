#!/bin/bash
# fuse_medgemma.sh
# Fuses the trained LoRA adapters back into the base MedGemma model

MODEL_PATH="../models/medgemma-1.5-4b-it"
ADAPTER_PATH="../adapters/medgemma-lora"
OUTPUT_PATH="../models/medgemma-1.5-4b-it-fused"

echo "[MLX] Fusing MedGemma adapters..."

python3=../.venv/bin/python3

$python3 -m mlx_lm.fuse \
    --model "$MODEL_PATH" \
    --adapter-path "$ADAPTER_PATH" \
    --save-path "$OUTPUT_PATH" \
    --dequantize

echo "[MLX] Fusion complete. Fused model saved to $OUTPUT_PATH"
