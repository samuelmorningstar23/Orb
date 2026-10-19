"""Deterministic lexical-store fixtures, independent of installed model weights."""
from contextlib import contextmanager

import pytest


@contextmanager
def lexical_knowledge_models():
    """Provide explicit seed embeddings, then exercise genuine lexical search.

    Seed-table construction requires one vector per document; the tests below
    evaluate FTS/lexical retrieval rather than a learned model. The temporary
    seed vectors never participate in queries: dense search is unavailable.
    App lifespan's ORB_SKIP_KNOWLEDGE_BASE setting remains intact outside the
    explicit seeding call, and no installed weights are loaded or downloaded.
    """
    from services import clinical_knowledge as ck, corpus_loader

    def unavailable(*args, **kwargs):
        raise RuntimeError("Synthetic lexical-only test profile")

    def seed_builtin():
        with pytest.MonkeyPatch.context() as seed:
            seed.delenv("ORB_SKIP_KNOWLEDGE_BASE", raising=False)
            seed.setattr(ck, "_embed_texts", lambda texts: [bytes(ck.EMBEDDING_DIM * 4) for _ in texts])
            ck.init_knowledge_base()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(ck, "_embed_texts", unavailable)
        patch.setattr(ck, "_cross_encoder", None)
        patch.setattr(ck, "_cross_encoder_checked", True)
        patch.setattr(corpus_loader, "_real_embedder_available", lambda: False)
        yield seed_builtin
