"""Orb Sage Voice Agent — Conversational Clinical AI (MedGemma 4B)

Provides real-time conversational clinical Q&A powered by MedGemma 1.5 4B
running natively on Apple Silicon via MLX-LM.

Architecture:
    Clinician speaks → Whisper daemon transcribes → Voice agent processes →
    Streaming response tokens → Client TTS playback

The agent automatically injects patient context (medications, vitals, diagnosis)
from the local SQLite database into its system prompt, giving it clinical
context that no cloud ASR API has access to.
"""
import contextvars
import os
import sys
import json
import time
import re
from typing import Optional, AsyncGenerator
from pathlib import Path

# Add parent dirs to path
SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVER_DIR))


# ── Streaming producer-thread lifetime ──
# The MLX producer runs on a plain thread while the UMSQ GPU gate is held. It is
# polled (rather than blocked on) so a wedged generation cannot hold the single
# GPU slot forever, and it is joined before the gate is released so an orphaned
# generation never overlaps the next GPU job.
_STREAM_POLL_SECONDS = 2.0          # how often a starved consumer wakes to re-check
_STREAM_WATCHDOG_SECONDS = 180.0    # hard cap on a stalled (no-token) stream
_PRODUCER_JOIN_SECONDS = 5.0        # grace for the producer to stop after cancel


# ── What the current voice turn was built from ──
# The agent is a process-wide singleton, so this cannot live on it; a
# ContextVar keeps it per request. The voice route opens a collector before
# streaming and records what fills it in provenance. Spoken answers used to
# carry no chart snapshot and no source trail at all while typed ones did.
_VOICE_RETRIEVAL: contextvars.ContextVar = contextvars.ContextVar("orb_voice_retrieval", default=None)

# R026, rulings A and E, on the spoken lane too. When retrieval writes the
# which-patient question, the voice lane SPEAKS THAT TEXT and never asks the
# model to (the typed lane does the same). When the answer comes from a chart
# the clinician did not name, its first words are that patient's name and bed.
# Per request, like the collector above.
_VOICE_DIRECT: contextvars.ContextVar = contextvars.ContextVar("orb_voice_direct", default=None)
_VOICE_LABEL: contextvars.ContextVar = contextvars.ContextVar("orb_voice_label", default=None)


def _as_history(conversation_history) -> list:
    """The spoken lane's [{query, response}] as retrieval's [{role, content}],
    so the ask-again rule and the conversation's patient work here too."""
    out = []
    for entry in (conversation_history or [])[-10:]:
        if not isinstance(entry, dict):
            continue
        out.append({"role": "user", "content": str(entry.get("query") or "")})
        out.append({"role": "assistant", "content": str(entry.get("response") or "")})
    return out


def collect_voice_retrieval() -> dict:
    """Start collecting this turn's retrieval; returns the dict that fills.

    Keys: `sources` (retrieved passages), `patient_context` (the chart the
    prompt was built from, by value), `prompt` (the system prompt, hashed into
    provenance) and `query_scope` (retrieval's verdict, so an OUT_OF_SCOPE
    turn records no patient, as the chat lane does).
    """
    record: dict = {"sources": [], "patient_context": None, "prompt": None,
                    "query_scope": None}
    _VOICE_RETRIEVAL.set(record)
    return record


def collect_voice_sources() -> list:
    """`collect_voice_retrieval`, returning only the record's `sources` list
    (kept for callers that need nothing else)."""
    return collect_voice_retrieval()["sources"]


# ── System Prompt Templates ──

VOICE_SYSTEM_PROMPT = """You are Sage, a clinical AI assistant at {hospital_name}. You respond to voice queries from clinicians during ward rounds, consultations, and emergencies.

CURRENT PATIENT CONTEXT:
{patient_context}

RULES:
1. Be concise: your response will be spoken aloud via TTS. Keep answers under 3 sentences unless asked for detail.
2. Always cite which patient data informed your response (e.g., "Based on their current Metoprolol prescription...").
3. For drug interactions, dosing, or critical decisions, always add "Please verify with pharmacy" or similar safety qualifier.
4. Never fabricate data. If the patient context doesn't contain the answer, say "I don't have that information in the current chart."
5. For emergency queries (cardiac arrest, anaphylaxis, stroke), respond with immediate protocol steps first, context second.
"""

VOICE_SYSTEM_PROMPT_NO_CONTEXT = """You are Sage, a clinical AI assistant. You respond to voice queries from clinicians.

No specific patient is currently selected. Answer general clinical questions concisely. Your response will be spoken aloud via TTS. Keep answers under 3 sentences unless asked for detail. Always add safety qualifiers for drug dosing and critical decisions."""


def build_patient_context(patient_id: Optional[str], user: dict) -> str:
    """Pull patient context from local SQLite for system prompt injection.

    `user` is REQUIRED and positional for the same reason it is on
    get_relevant_context: this function assembles demographics, medications,
    vitals and risk for whatever patient_id it is given, and it is reached as
    the FALLBACK when get_relevant_context raises — i.e. exactly when the
    primary scoping path did not run. A fallback that is not scoped is a hole
    that only opens when something else is already broken, which is the hardest
    kind to notice.
    """
    if not patient_id:
        return ""

    try:
        from database import get_db
        from services.sage_service import _patient_in_scope
        from permissions import department_scope
        db = get_db()

        # ONE derivation, same as every other patient surface.
        if not _patient_in_scope(db, patient_id, department_scope(user or {})):
            db.close()
            return "That patient is not in your department; no chart data is available to you."

        parts = []
        
        # Patient demographics + diagnosis
        # NOTE: get_db() sets row_factory = sqlite3.Row, which has NO .get()
        # method — calling .get() on a Row raises AttributeError. Convert every
        # fetched row to a plain dict before using .get() with defaults.
        patient_row = db.execute(
            "SELECT name, age, sex, diagnosis, bed, ward FROM patients WHERE id = ?",
            (patient_id,),
        ).fetchone()
        if patient_row:
            patient = dict(patient_row)
            parts.append(f"Patient: {patient.get('name', 'N/A')}, {patient.get('age', 'N/A')}y {patient.get('sex', '')}")
            parts.append(f"Location: Ward {patient.get('ward', 'N/A')}: Bed {patient.get('bed', 'N/A')}")
            if patient.get("diagnosis"):
                parts.append(f"Diagnosis: {patient['diagnosis']}")

        # Active medications
        # Schema column is `dosage` (database.py), not `dose` — selecting `dose`
        # raised OperationalError: no such column.
        meds = db.execute(
            "SELECT drug_name, dosage, route, frequency FROM medications WHERE patient_id = ? AND status != 'discontinued'",
            (patient_id,),
        ).fetchall()
        if meds:
            med_list = []
            for row in meds:
                m = dict(row)
                med_list.append(
                    f"  - {m.get('drug_name', '')} {m.get('dosage', '')} {m.get('route', '')} {m.get('frequency', '')}"
                )
            parts.append("Active Medications:\n" + "\n".join(med_list))

        # Latest vitals
        vitals_row = db.execute(
            "SELECT heart_rate, systolic_bp, diastolic_bp, spo2, temperature, respiratory_rate, timestamp FROM vitals WHERE patient_id = ? AND voided_at IS NULL ORDER BY timestamp DESC, id DESC LIMIT 1",
            (patient_id,),
        ).fetchone()
        if vitals_row:
            vitals = dict(vitals_row)
            # AS CHARTED (services/vital_charting.py), "--" when not recorded:
            # the NEWS2 line below is scored on these numbers, so a raw "92.4"
            # here would sit beside a driver that says 92.
            from services.vital_charting import charted_text
            parts.append(
                f"Latest Vitals ({vitals.get('timestamp', 'recent')}): "
                f"HR {charted_text(vitals.get('heart_rate'))}, "
                f"BP {charted_text(vitals.get('systolic_bp'))}/{charted_text(vitals.get('diastolic_bp'))}, "
                f"SpO2 {charted_text(vitals.get('spo2'), unit='%')}, "
                f"Temp {charted_text(vitals.get('temperature'), 1, unit='°C')}, "
                f"RR {charted_text(vitals.get('respiratory_rate'))}"
            )

        # NEWS2 integer and band, never the 0-1 risk index (2026-10-03: this
        # line was "Risk: Score 0.55", and a model reads 0.55 as a 55% chance).
        # The SAME block and renderer as Sage's chart context, so voice and the
        # Sage panel cannot state two different scores for one patient. With no
        # observation to score, the stored row's band is all there is.
        from services.sage_service import _fresh_risk_block, _risk_summary_line
        risk_block = _fresh_risk_block(db, patient_id)
        if risk_block is None:
            risk_row = db.execute(
                "SELECT level, drivers FROM risk_scores WHERE patient_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                (patient_id,),
            ).fetchone()
            if risk_row:
                risk_block = {"news2": None, "level": risk_row["level"],
                              "drivers": risk_row["drivers"]}
        if risk_block:
            parts.append(_risk_summary_line(risk_block))

        db.close()
        return "\n".join(parts) if parts else "No patient data available."
    
    except Exception as e:
        print(f"[VoiceAgent] Error building patient context: {e}")
        return "Error loading patient data."


class VoiceStreamCleaner:
    def __init__(self):
        self.raw_buffer = ""
        self.header_stripped = False
        self.terminated = False
        self.yielded_len = 0
        self.content_start_idx = 0

    def feed(self, chunk: str) -> str:
        if self.terminated:
            return ""
            
        self.raw_buffer += chunk
        
        # 1. Strip starting scaffolding/headers if not done yet
        if not self.header_stripped:
            # Check for explicit tags/markers first
            markers = [
                "<concise_answer>", "concise_answer:",
                "Response:", "Answer:", "Result:"
            ]
            marker_found = False
            for marker in markers:
                if marker.lower() in self.raw_buffer.lower():
                    idx = self.raw_buffer.lower().index(marker.lower())
                    self.content_start_idx = idx + len(marker)
                    self.header_stripped = True
                    marker_found = True
                    break
                    
            if not marker_found:
                scaffolds = ["Thought:", "Context:", "Clinical Assessment:", "Extracted clinical analysis:", "Analysis:"]
                matched_scaffold = None
                for s in scaffolds:
                    # Match scaffold pattern at the very start of raw_buffer
                    pattern = rf'^\s*{re.escape(s)}\s*'
                    match = re.match(pattern, self.raw_buffer, re.IGNORECASE)
                    if match:
                        matched_scaffold = match.group(0)
                        break
                
                if matched_scaffold:
                    # If we matched a scaffold, strip it immediately!
                    self.content_start_idx = len(matched_scaffold)
                    self.header_stripped = True
                else:
                    # Check if raw_buffer is a prefix of any scaffold (still typing the scaffold name)
                    buffer_stripped = self.raw_buffer.strip().lower()
                    is_prefix = False
                    for s in scaffolds:
                        if s.lower().startswith(buffer_stripped):
                            is_prefix = True
                            break
                    
                    # If it's not a prefix of any scaffold, and we've accumulated some text,
                    # then it's normal content. Mark as stripped.
                    if not is_prefix and len(self.raw_buffer.strip()) > 0:
                        self.content_start_idx = 0
                        self.header_stripped = True

        if not self.header_stripped:
            return ""

        # Extract active text
        active_text = self.raw_buffer[self.content_start_idx:]

        # Clean xml/markdown tags from active output
        cleaned_text = active_text
        for tag in ["<concise_answer>", "</concise_answer>", "<action_intent>", "</action_intent>", "<clinical_assessment>", "</clinical_assessment>", "<caveats>", "</caveats>", "<escalation>", "</escalation>"]:
            cleaned_text = cleaned_text.replace(tag, "")

        # 2. Check for repeating character loops (like infinite commas, spaces, dots)
        if re.search(r'(,\s*){4,}', cleaned_text):
            self.terminated = True
            cleaned_text = re.split(r'(,\s*){4,}', cleaned_text)[0].strip()
            # Clean up trailing scaffolding words that lead into the loop
            for trail in ["full transcription:", "transcription:", "transcript:", "full transcript:", "context:"]:
                if cleaned_text.lower().endswith(trail):
                    cleaned_text = cleaned_text[:-len(trail)].strip()
                elif cleaned_text.lower().endswith(trail.strip(":")):
                    cleaned_text = cleaned_text[:-len(trail.strip(":"))].strip()
            
        elif re.search(r'(\.\s*){5,}', cleaned_text):
            self.terminated = True
            cleaned_text = re.split(r'(\.\s*){5,}', cleaned_text)[0].strip()
            for trail in ["full transcription:", "transcription:", "transcript:", "full transcript:", "context:"]:
                if cleaned_text.lower().endswith(trail):
                    cleaned_text = cleaned_text[:-len(trail)].strip()
                elif cleaned_text.lower().endswith(trail.strip(":")):
                    cleaned_text = cleaned_text[:-len(trail.strip(":"))].strip()

        # Check for repeating phrase/word loops
        if not self.terminated:
            words = cleaned_text.split()
            if len(words) >= 8:
                last_3 = words[-3:]
                if words[-6:-3] == last_3:
                    self.terminated = True
                    idx = cleaned_text.rfind(last_3[0])
                    if idx != -1:
                        cleaned_text = cleaned_text[:idx].strip()
                elif len(words) >= 10:
                    last_4 = words[-4:]
                    if words[-8:-4] == last_4:
                        self.terminated = True
                        idx = cleaned_text.rfind(last_4[0])
                        if idx != -1:
                            cleaned_text = cleaned_text[:idx].strip()

        # Yield new tokens
        if len(cleaned_text) > self.yielded_len:
            to_yield = cleaned_text[self.yielded_len:]
            self.yielded_len = len(cleaned_text)
            return to_yield
        else:
            return ""


class VoiceAgent:
    """Conversational clinical AI agent powered by MedGemma 4B via MLX-LM."""
    
    def __init__(self, model_path: Optional[str] = None, adapter_path: Optional[str] = None,
                 max_tokens: int = 256, temperature: float = 0.3):
        self.model_path = model_path or str(SERVER_DIR / "models" / "medgemma-1.5-4b-it")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.hospital_name = os.getenv("ORB_HOSPITAL_NAME", "Orb Hospital")
        
        # Auto-detect QLoRA adapters (trained on MTSamples + Indian clinical vocab)
        default_adapter_dir = SERVER_DIR / "adapters"
        if adapter_path:
            self.adapter_path = adapter_path
        elif (default_adapter_dir / "adapters.safetensors").exists():
            self.adapter_path = str(default_adapter_dir)
        else:
            self.adapter_path = None
        
        self._model = None
        self._tokenizer = None
        self._loaded = False
    
    def load(self):
        """Load MedGemma 4B (+ QLoRA adapters if available) into Apple Silicon Unified Memory.
        Refused while the models are switched off (ORB_MODELS_ENABLED, or the
        Orb Ward Record)."""
        from services.model_switch import refuse_when_models_off
        refuse_when_models_off("voice agent load")
        if self._loaded:
            return
        
        if not os.path.exists(self.model_path):
            print(f"[VoiceAgent] Model not found at {self.model_path}")
            print("[VoiceAgent] Download with: huggingface-cli download google/medgemma-1.5-4b-it")
            return
        
        adapter_label = ""
        if self.adapter_path:
            adapter_label = f" + QLoRA adapters ({self.adapter_path})"
        print(f"[VoiceAgent] Loading MedGemma 4B from {self.model_path}{adapter_label}...")
        t0 = time.time()
        
        try:
            from mlx_lm import load
            self._model, self._tokenizer = load(
                self.model_path,
                adapter_path=self.adapter_path,
            )
            self._loaded = True
            elapsed = time.time() - t0
            suffix = " (with fine-tuned adapters)" if self.adapter_path else ""
            print(f"[VoiceAgent] Model loaded in {elapsed:.1f}s{suffix} — ready for conversation")
        except ImportError:
            print("[VoiceAgent] mlx-lm not installed. Install with: pip install mlx-lm")
        except Exception as e:
            print(f"[VoiceAgent] Failed to load model: {e}")
    
    def _build_system_prompt(self, query: str, user: dict, patient_id: Optional[str] = None, is_follow_up: bool = False, is_first_turn: bool = False, history: Optional[list] = None) -> str:
        """Build system prompt with relevant context, scoped to `user`.

        `user` is required and positional — the spoken lane reads the same
        charts as the typed one and gets the same department boundary. A
        guardrail present on only one surface is how a hole ships (see the
        forbidden-phrase sanitizer's history in routes/sage_routes.py).
        """
        base_prompt = ""
        record = _VOICE_RETRIEVAL.get()
        try:
            from services.sage_service import get_relevant_context
            context, scope, sources, confidence, escalation, patient_context_dict, query_scope = get_relevant_context(query, user, patient_id, history)
            # Spoken as written, no model: the which-patient question, and the
            # notice for a request a reply did not carry over (bed locator
            # attempt 13e; review 13d found the notice handed to the model).
            if query_scope in ("DISAMBIGUATE", "NOT_CARRIED"):
                _VOICE_DIRECT.set(context)
            elif isinstance(patient_context_dict, dict) and patient_context_dict.get("answer_label"):
                _VOICE_LABEL.set(patient_context_dict["answer_label"])
            # What the spoken answer was built from, for the turn's provenance.
            if record is not None:
                record["sources"].extend(sources or [])
                record["patient_context"] = patient_context_dict
                record["query_scope"] = query_scope

            if query_scope == "OUT_OF_SCOPE":
                # Retrieval refused and has already written the security audit
                # row. Return IMMEDIATELY — falling through to the
                # build_patient_context fallback below would rebuild the very
                # chart that was just denied.
                return (
                    f"You are Sage, a clinical AI assistant at {self.hospital_name}.\n"
                    "The clinician asked about a patient who is NOT in their department. "
                    "You have NO information about that patient and must not guess, infer or "
                    "invent any.\n\n"
                    "Say, briefly and in your own words, that the patient is outside their "
                    "department so you cannot share anything from that chart, that this is not "
                    "the same as the patient not existing, and that they should contact the team "
                    "caring for them or an administrator. Two sentences maximum. Add nothing else."
                )

            if query_scope == "DISAMBIGUATE":
                base_prompt = f"""You are Sage, a clinical AI assistant at {self.hospital_name}.
The user mentioned a patient, but multiple patients match that description.

MATCHING PATIENTS:
{context}

RULES:
1. DO NOT HALLUCINATE or guess which patient they mean.
2. Ask the user to clarify which patient they mean by listing the options clearly (e.g. "I found two patients named Smith. Did you mean John in Bed 1 or Mary in Bed 2?").
3. Keep it brief.
"""
            elif query_scope == "BROAD":
                base_prompt = f"""You are Sage, a clinical AI assistant at {self.hospital_name}. You respond to voice queries from clinicians.
                
Below is the department clinical census (admitted patients, locations, vitals, risks, and medications):
{context}

RULES:
1. Be concise: your response will be spoken aloud via TTS. Keep answers under 3 sentences unless asked for detail.
2. Summarize the requested department or patient status accurately.
3. Keep it conversational and professional.
"""
            elif context:
                base_prompt = VOICE_SYSTEM_PROMPT.format(
                    hospital_name=self.hospital_name,
                    patient_context=context,
                )
        except Exception as e:
            print(f"[VoiceAgent] Error getting relevant context: {e}")

        if not base_prompt:
            if patient_id:
                context = build_patient_context(patient_id, user)
                # This fallback reads the open chart itself, so it is named
                # first like any other chart the clinician did not name
                # (ruling E). build_patient_context already refuses a chart
                # outside the department; the label helper checks again.
                if context and not _VOICE_LABEL.get():
                    from services.sage_service import answer_label_for_patient
                    _label = answer_label_for_patient(patient_id, user)
                    if _label:
                        _VOICE_LABEL.set(_label)
                base_prompt = VOICE_SYSTEM_PROMPT.format(
                    hospital_name=self.hospital_name,
                    patient_context=context,
                )
                if record is not None and record["patient_context"] is None:
                    # The fallback chart is text, not the assembled dict; it is
                    # still what the model was shown, so it is kept by value.
                    record["patient_context"] = {"patient_id": patient_id,
                                                 "voice_chart_text": context}
            else:
                base_prompt = VOICE_SYSTEM_PROMPT_NO_CONTEXT
                
        # Inject dynamic modifiers
        modifiers = []
        if is_first_turn:
            import datetime
            hour = datetime.datetime.now().hour
            greeting = "Good morning" if hour < 12 else "Good afternoon" if hour < 17 else "Good evening"
            modifiers.append(f"Start your response with a brief greeting (e.g. {greeting}).")
            
        if is_follow_up:
            modifiers.append("This is a follow-up question. Be extremely concise (1-2 sentences maximum) unless detail is explicitly requested.")

        if modifiers:
            base_prompt += "\n\nADDITIONAL INSTRUCTIONS:\n" + "\n".join(f"- {m}" for m in modifiers)

        if record is not None:
            record["prompt"] = base_prompt
        return base_prompt
    
    def generate(self, query: str, user: dict, patient_id: Optional[str] = None) -> str:
        """Synchronous generation — returns full response."""
        _VOICE_DIRECT.set(None)
        _VOICE_LABEL.set(None)
        system_prompt = self._build_system_prompt(query, user, patient_id, is_follow_up=False, is_first_turn=True)
        if _VOICE_DIRECT.get():
            return _VOICE_DIRECT.get()
        if not self._loaded:
            self.load()
        if not self._loaded:
            return "Voice agent model not available."
        
        from mlx_lm import generate as mlx_generate
        from mlx_lm.sample_utils import make_sampler
        from services.gpu_coordinator import vram_guard
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query}
        ]
        prompt = self._tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        
        t0 = time.time()
        with vram_guard("Sage Voice (sync)"):
            response = mlx_generate(
                self._model,
                self._tokenizer,
                prompt=prompt,
                max_tokens=self.max_tokens,
                sampler=make_sampler(temp=self.temperature),
            )
        elapsed_ms = int((time.time() - t0) * 1000)
        
        # Clean response
        cleaner = VoiceStreamCleaner()
        cleaned_response = cleaner.feed(response)
        final_flush = cleaner.feed("")
        if final_flush:
            cleaned_response += final_flush
        
        print(f"[VoiceAgent] Generated {len(cleaned_response)} cleaned chars in {elapsed_ms}ms")
        _label = _VOICE_LABEL.get()
        return f"{_label} {cleaned_response.strip()}" if _label else cleaned_response.strip()
    
    async def generate_stream(self, query: str, user: dict, patient_id: Optional[str] = None, conversation_history: Optional[list] = None) -> AsyncGenerator[str, None]:
        """Async streaming generation — yields token strings for real-time TTS.
        
        Acquires the UMSQ GPU lock at PRIORITY_SAGE (1) — highest priority.
        This guarantees Sage voice queries always preempt background Vigil/Forecast scoring.
        """
        # Retrieval first: a which-patient turn is answered by the server's own
        # question and never reaches (or loads) the model.
        _VOICE_DIRECT.set(None)
        _VOICE_LABEL.set(None)
        is_first_turn = not bool(conversation_history)
        is_follow_up = bool(conversation_history)
        system_prompt = self._build_system_prompt(query, user, patient_id, is_follow_up, is_first_turn,
                                                  history=_as_history(conversation_history))
        if _VOICE_DIRECT.get():
            yield _VOICE_DIRECT.get()
            return

        if not self._loaded:
            self.load()
        if not self._loaded:
            yield "Voice agent model not available."
            return
        
        import asyncio
        import queue
        import threading
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler
        from services.gpu_coordinator import umsq, vram_guard, PRIORITY_SAGE

        # Ruling E: the patient's name and bed are the first words spoken when
        # the clinician did not name them.
        if _VOICE_LABEL.get():
            yield _VOICE_LABEL.get() + " "
        
        messages = [
            {"role": "system", "content": system_prompt},
        ]
        # Inject conversation history for follow-up context
        if conversation_history:
            for entry in conversation_history[-10:]:  # Keep last 10 exchanges
                messages.append({"role": "user", "content": entry.get("query", "")})
                messages.append({"role": "assistant", "content": entry.get("response", "")})
        messages.append({"role": "user", "content": query})
        prompt = self._tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        
        sampler = make_sampler(temp=self.temperature)
        t0 = time.time()
        first_token = True
        
        q = queue.Queue()
        # Cancellation flag for the producer. stream_generate can only observe it
        # BETWEEN tokens, so a hard-wedged step still needs the watchdog below.
        stop = threading.Event()

        def _producer():
            with vram_guard("Sage Voice (stream)"):
                try:
                    for resp in stream_generate(
                        self._model,
                        self._tokenizer,
                        prompt=prompt,
                        max_tokens=self.max_tokens,
                        sampler=sampler,
                    ):
                        if stop.is_set():
                            break
                        # resp is a GenerationResponse; extract the text segment
                        q.put(resp.text)
                except Exception as e:
                    q.put(e)
                finally:
                    q.put(None)

        token_count = 0
        # Acquire priority GPU lock BEFORE spawning the producer thread
        async with umsq.acquire("Sage Voice", priority=PRIORITY_SAGE):
            # Start generator in background thread (GPU lock is held)
            thread = threading.Thread(target=_producer, daemon=True)
            thread.start()

            # Read from queue in event loop thread pool
            loop = asyncio.get_event_loop()
            cleaner = VoiceStreamCleaner()
            deadline = time.time() + _STREAM_WATCHDOG_SECONDS

            try:
                while True:
                    try:
                        item = await loop.run_in_executor(
                            None, q.get, True, _STREAM_POLL_SECONDS
                        )
                    except queue.Empty:
                        # Watchdog: a wedged MLX call must not hold the UMSQ slot
                        # (and therefore the single GPU) indefinitely.
                        if time.time() >= deadline:
                            print(f"[VoiceAgent] Stream watchdog fired after {_STREAM_WATCHDOG_SECONDS}s; cancelling generation")
                            break
                        continue
                    if item is None:
                        final_flush = cleaner.feed("")
                        if final_flush:
                            yield final_flush
                        break
                    if isinstance(item, Exception):
                        print(f"[VoiceAgent] Error in stream: {item}")
                        yield f" [Error: {str(item)}] "
                        break

                    if first_token:
                        ttft = int((time.time() - t0) * 1000)
                        print(f"[VoiceAgent] Time-to-first-token: {ttft}ms")
                        first_token = False

                    token_count += 1
                    cleaned = cleaner.feed(item)
                    if cleaned:
                        yield cleaned

                    if cleaner.terminated:
                        print("[VoiceAgent] Streaming terminated early due to loop/scaffolding detection")
                        break
            finally:
                # The GPU gate is released when this `async with` exits, so the
                # producer MUST be stopped first — otherwise an orphaned
                # generation keeps running on Metal while the next job acquires
                # the gate, breaking the one-GPU-job invariant. Covers normal
                # completion, the cleaner.terminated break, the watchdog, and
                # GeneratorExit from a client disconnect.
                stop.set()
                if thread.is_alive():
                    await loop.run_in_executor(None, thread.join, _PRODUCER_JOIN_SECONDS)
                    if thread.is_alive():
                        print("[VoiceAgent] Producer thread did not stop within "
                              f"{_PRODUCER_JOIN_SECONDS}s; GPU gate released with generation still running")

        total = int((time.time() - t0) * 1000)
        print(f"[VoiceAgent] Stream complete: {total}ms total, {token_count} raw tokens processed")


# ── Singleton instance ──
_voice_agent: Optional[VoiceAgent] = None


def get_voice_agent() -> VoiceAgent:
    """Get or create the global voice agent instance."""
    global _voice_agent
    if _voice_agent is None:
        _voice_agent = VoiceAgent()
    return _voice_agent


if __name__ == "__main__":
    # Quick CLI test
    agent = VoiceAgent()
    agent.load()
    
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = "What are the common side effects of Metoprolol?"
    
    print(f"\nQuery: {query}")
    # CLI smoke test: no authenticated user, so no department -> hospital-wide,
    # the same convention permissions.department_scope() applies to a
    # governance account with no department on its row.
    sys_prompt = agent._build_system_prompt(query, {})
    print(f"\n--- DEBUG SYSTEM PROMPT ---\n{sys_prompt}\n--------------------------\n")
    print(f"Response: {agent.generate(query, {})}")
