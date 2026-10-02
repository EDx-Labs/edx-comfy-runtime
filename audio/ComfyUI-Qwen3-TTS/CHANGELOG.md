# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.7.0] - 2026-01-31

### Added
- Support for other audio loading nodes (e.g., base64 to audio for ComfyUI endpoints) (PR #24)
- `checkpoint_path` parameter to Qwen3Loader for loading intermediate training checkpoints
- `save_optimizer_state` option to control checkpoint size (disabled = ~50% smaller checkpoints)
- Automatic bf16→fp32 fallback for pre-Ampere GPUs
- Easy voice clone workflow example

### Changed
- Unified checkpoint format using `pytorch_model.bin` only for consistency
- Split checkpoint saving into `save_training_checkpoint()` (lightweight resume) and `save_final_model()` (inference-ready)
- Intermediate checkpoints renamed to `ckpt_step_N` and `ckpt_epoch_N` to distinguish from final models
- AudioCompare now uses `safe_open()` for memory-efficient speaker encoder loading (~3.4GB → ~300KB RAM)
- Removed fp16 from mixed_precision options (only bf16/fp32 supported)

### Fixed
- DDP/FSDP compatibility: unwrap model for attribute access in training
- Resume training from `ckpt_step_N` checkpoint folders
- Total steps calculation now uses optimizer update steps (fixes progress display with gradient accumulation)
- Auto-detect checkpoint vs full model path in Qwen3Loader
- Speaker mapping from checkpoint config now applies correctly
- `tts_model_type` injection for Base-trained checkpoints
- RANK environment variable errors on single-GPU setups

### Contributors
- @bmgjet - Audio loading node support (PR #24)
- @rekuenkdr - Checkpoint refactoring, DDP fixes, AudioCompare optimizations (PR #27)

## [1.6.1] - 2026-01-29

### Added
- `local_model_path` parameter to Audio Compare node (fixes #21)
- Step-based checkpoint saving with `save_every_steps` parameter
- `log_every_steps` parameter for training progress control
- Learning rate display in training progress output

### Changed
- Replaced `keep_intermediate_checkpoints` with `save_every_epochs` and `save_every_steps` for finer control
- Training progress now shows optimizer steps (accounts for gradient accumulation)
- Step counting uses `math.ceil()` to prevent 0 steps for small datasets

### Fixed
- Audio Compare cache now correctly invalidates when switching `local_model_path`
- Step-based logging/saving now triggers at correct intervals with gradient accumulation

### Contributors
- @rekuenkdr - local_model_path fix, step-based checkpointing

## [1.6.0] - 2026-01-27

### Added
- **New nodes: Qwen3-TTS Save Prompt & Load Prompt** for persistent voice embeddings
  - Save voice clone prompts to `models/Qwen3-TTS/prompts/` as safetensors files
  - Load saved prompts directly without recomputing from audio
  - Enables building reusable voice libraries

## [1.5.0] - 2026-01-27

### Added
- `max_new_tokens` parameter for Voice Clone node to control generation length and prevent hangs
- `ref_audio_max_seconds` parameter to auto-trim long reference audio (default: 30s)
- Troubleshooting section in README for common issues
- **New node: Qwen3-TTS Audio Compare** for evaluating fine-tuned models (speaker similarity, mel spectrogram distance, speaking rate)
- Resume training from checkpoints with `resume_training` parameter
- Per-epoch checkpointing with automatic cleanup of intermediate checkpoints
- 8-bit AdamW optimizer support via bitsandbytes for reduced VRAM usage
- Gradient checkpointing option (~30-40% VRAM savings during training)
- Learning rate warmup scheduling with state persistence
- SHA256-based dataset caching to skip reprocessing unchanged datasets
- Configurable `batch_size` parameter in Data Prep node for VRAM control
- Support for 0.6B model fine-tuning (text_projection fix)
- UI progress status updates with loss tracking during training
- Optimizer and scheduler state saving for true training resume

### Changed
- Reduced default `max_new_tokens` from 8192 to 2048 to prevent generation hangs
- Reduced default learning rate from 2e-5 to 2e-6 for training stability
- Updated documentation with generation hang mitigation tips and new fine-tuning features

### Fixed
- Mitigated infinite generation loop issue when using long reference audio or generating long outputs
- Fixed dtype parameter handling in fine-tuning
- Fixed speaker embedding CPU conversion issue

### Contributors
- @rekuenkdr - Training enhancements, VRAM optimizations, Audio Compare node

## [1.4.0] - 2026-01-24

### Added
- Exposed finetuning seed parameter for reproducible training
- Resource cleanup for mixed workflows

### Changed
- Improved memory management when switching between inference and training

## [1.3.0] - 2026-01-24

### Changed
- Updated model download management with improved caching and error handling
- Enhanced README documentation

## [1.2.0] - 2026-01-24

### Added
- RNG seed support for reproducible audio generation in inference nodes

## [1.1.0] - 2026-01-23

### Added
- Fine-tuning support with dataset handling (`dataset.py`)
- Simple finetuning example workflow
- MPS (Apple Silicon) device support

### Changed
- Use ComfyUI `model_management` for device detection
- Updated transformers dependency warning
- Updated requirements with safetensors

### Fixed
- Project name in pyproject.toml to match repository naming convention

## [1.0.0] - 2026-01-22

### Added
- Initial release
- Qwen3-TTS model loader node
- Text-to-speech generation node
- Custom voice workflow configuration
- Flash attention support with automatic fallback
