"""Metadata enrichment: provider registry, matching, and enrichment engine."""

from audiobookdl.utils.enrichment.engine import (
    MAX_COVER_BYTES as MAX_COVER_BYTES,
    apply_candidate_to_audiobook as apply_candidate_to_audiobook,
    enrich_audiobook_metadata as enrich_audiobook_metadata,
    search_enrichment_candidates as search_enrichment_candidates,
)
from audiobookdl.utils.enrichment.matching import (
    score_candidate_match as score_candidate_match,
)
from audiobookdl.utils.enrichment.models import (
    CUSTOM_PROVIDER_PREFIX as CUSTOM_PROVIDER_PREFIX,
    DEFAULT_CONFIDENCE_THRESHOLD as DEFAULT_CONFIDENCE_THRESHOLD,
    DEFAULT_PROVIDERS as DEFAULT_PROVIDERS,
    ENRICHMENT_ENTRY_POINT_GROUP as ENRICHMENT_ENTRY_POINT_GROUP,
    PLUGIN_PROVIDER_PREFIX as PLUGIN_PROVIDER_PREFIX,
    PROVIDER_ALIASES as PROVIDER_ALIASES,
    CandidateSelector as CandidateSelector,
    EnrichmentCandidate as EnrichmentCandidate,
    EnrichmentResult as EnrichmentResult,
)
from audiobookdl.utils.enrichment.providers.abs_custom import (
    test_custom_metadata_provider as test_custom_metadata_provider,
)
from audiobookdl.utils.enrichment.providers.registry import (
    normalize_custom_provider_name as normalize_custom_provider_name,
    parse_provider_list as parse_provider_list,
    reset_plugin_enrichment_providers_cache as reset_plugin_enrichment_providers_cache,
    resolve_provider_list as resolve_provider_list,
    resolve_redirect_url as resolve_redirect_url,
    validate_custom_provider_url as validate_custom_provider_url,
)
