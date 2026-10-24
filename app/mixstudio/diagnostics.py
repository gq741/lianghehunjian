"""Small, allowlisted failure metadata. Never stores payloads or raw responses."""
import re

_CODES = frozenset({'provider_error', 'invalid_input', 'invalid_key', 'cancelled',
    'timeout', 'network_timeout', 'dns', 'network_dns', 'tls', 'network_tls',
    'connection', 'network', 'network_interrupted', 'invalid_response',
    'invalid_vision_response', 'response_too_large', 'request_too_large',
    'invalid_vision_input', 'invalid_cache', 'request_limit', 'invalid_frame',
    'source_changed', 'cache_write', 'invalid_audio', 'voice_result_unknown',
    'asr_failed', 'download_failed', 'download_limit', 'download_tls',
    'download_transport', 'file_error', 'insufficient_material', 'invalid_download',
    'invalid_transcript', 'invalid_url', 'missing_dependency', 'missing_model',
    'output_exists', 'process_output', 'process_start', 'process_stop'})
_ISSUES = frozenset({'missing_ids', 'duplicate_ids', 'unknown_ids', 'json_invalid',
    'truncated', 'blocked', 'schema_invalid'})
_TYPES = frozenset({'object', 'array', 'string', 'number', 'boolean', 'null',
    'missing', 'invalid_json'})
_SHAPE_KEYS = frozenset({'response', 'choices', 'message', 'content', 'content_json',
    'observations'})
_FIELDS = frozenset({'response', 'choices', 'finish_reason', 'message', 'content',
    'observations', 'observation', 'clip_id', 'description', 'ocr', 'reason',
    'tags', 'flags', 'confidence', 'same_product'})
_RULES = frozenset({'object', 'single', 'stop', 'text', 'fields',
    'missing_observations', 'array', 'identity', 'range', 'enum', 'missing',
    'length', 'empty', 'control', 'count', 'duplicate'})
_SCRIPT_FIELDS = frozenset({'response', 'drafts', 'draft', 'title', 'voiceover',
    'segments', 'segment_id', 'text', 'visual_hint', 'fact_quotes'})
_TLS_REASONS = frozenset({'certificate_verify_failed', 'unexpected_eof',
    'connection_closed', 'handshake_failure', 'protocol_version', 'wrong_version', 'other'})


def normalize_diagnostic(value):
    """Return a detached JSON-safe subset; unknown keys/values are discarded."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    code = value.get('code')
    if isinstance(code, str) and (code in _CODES or re.fullmatch(r'http_[1-5][0-9]{2}', code)):
        clean['code'] = code
    if type(value.get('cleanup_failed')) is bool:
        clean['cleanup_failed'] = value['cleanup_failed']
    for name, maximum, minimum in (('http_status', 599, 100),
            ('elapsed_ms', 3600000, 0), ('response_bytes', 16 * 1024 * 1024, 0)):
        number = value.get(name)
        if type(number) is int and minimum <= number <= maximum:
            clean[name] = number
    business = value.get('business_code')
    if type(business) is int and 0 <= business <= 99999999:
        business = str(business)
    if isinstance(business, str) and re.fullmatch(r'[0-9]{1,8}', business):
        clean['business_code'] = business
    request_id = value.get('request_id')
    if (isinstance(request_id, str) and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', request_id) and
            not request_id.lower().startswith(('sk-', 'bearer'))):
        clean['request_id'] = request_id
    phase = value.get('request_phase')
    if isinstance(phase, str) and phase in ('connect', 'read'):
        clean['request_phase'] = phase
    tls_reason = value.get('tls_reason')
    if isinstance(tls_reason, str) and tls_reason in _TLS_REASONS:
        clean['tls_reason'] = tls_reason
        verify_code = value.get('tls_verify_code')
        if (tls_reason == 'certificate_verify_failed' and
                type(verify_code) is int and 0 <= verify_code <= 65535):
            clean['tls_verify_code'] = verify_code
    digest = value.get('response_sha256')
    if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
        clean['response_sha256'] = digest
    issue = value.get('response_issue')
    if isinstance(issue, str) and issue in _ISSUES:
        clean['response_issue'] = issue
    batch = value.get('vision_batch')
    if (isinstance(batch, (tuple, list)) and len(batch) == 3 and
            all(type(n) is int for n in batch) and
            1 <= batch[0] <= batch[1] <= 90 and 1 <= batch[2] <= 2):
        clean['vision_batch'] = list(batch)
    detail = value.get('schema_detail')
    if (isinstance(detail, (tuple, list)) and len(detail) == 3 and
            type(detail[0]) is int and 0 <= detail[0] <= 2 and
            isinstance(detail[1], str) and detail[1] in _FIELDS and
            isinstance(detail[2], str) and detail[2] in _RULES):
        clean['schema_detail'] = list(detail)
    # Script indices refer to a whole response (0) or one of at most five
    # requested drafts. Keep this separate from the two-window vision schema.
    detail = value.get('script_detail')
    if (isinstance(detail, (tuple, list)) and len(detail) == 3 and
            type(detail[0]) is int and 0 <= detail[0] <= 5 and
            isinstance(detail[1], str) and detail[1] in _SCRIPT_FIELDS and
            isinstance(detail[2], str) and detail[2] in _RULES):
        clean['script_detail'] = list(detail)
    counts = value.get('script_counts')
    if (isinstance(counts, (tuple, list)) and len(counts) == 2 and
            all(type(number) is int for number in counts) and
            1 <= counts[0] <= 5 and 0 <= counts[1] <= 10000):
        clean['script_counts'] = list(counts)
    shape = value.get('response_shape')
    if isinstance(shape, dict):
        safe_shape = {}
        for key in _SHAPE_KEYS:
            item = shape.get(key)
            if isinstance(item, str) and item in _TYPES:
                safe_shape[key] = item
        for key in ('choices_count', 'observations_count'):
            item = shape.get(key)
            if type(item) is int and 0 <= item <= 10000:
                safe_shape[key] = item
        finish = shape.get('finish_reason')
        if isinstance(finish, str) and finish in ('stop', 'length', 'content_filter', 'other', 'missing'):
            safe_shape['finish_reason'] = finish
        if safe_shape:
            clean['response_shape'] = safe_shape
    return clean


def copy_diagnostic(source, target):
    """Copy metadata across exception wrappers without forwarding arbitrary data."""
    value = normalize_diagnostic(getattr(source, 'diagnostic', None))
    value.update(normalize_diagnostic({'code': getattr(source, 'code', None)}))
    target.diagnostic = value
    return target
