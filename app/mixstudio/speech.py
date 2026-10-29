"""Small provider selector; no implicit failover or paid retries."""
import re


def selection(settings):
    from . import providers
    provider = settings.get('voice_provider', 'fish')
    if provider == 'fish':
        return {'provider':'fish', 'label':'Fish Audio', 'model':providers.FISH_MODEL,
                'voice_id':settings['voice_id']}
    if provider == 'minimax':
        model = settings.get('minimax_model', 'speech-2.8-hd')
        if model not in ('speech-2.8-hd', 'speech-2.8-turbo'):
            raise ValueError('MiniMax 配音模型无效，请在设置中重新选择')
        voice = settings.get('minimax_voice_id', 'male-qn-qingse')
        if (not isinstance(voice, str) or not voice.strip() or len(voice) > 256
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in voice)):
            raise ValueError('MiniMax 音色ID格式无效')
        return {'provider':'minimax', 'label':'MiniMax', 'model':model,
                'voice_id':voice.strip()}
    raise ValueError('配音服务无效，请选择 Fish Audio 或 MiniMax')


def validate(settings):
    chosen = selection(settings)
    voice = chosen['voice_id']
    if chosen['provider'] == 'fish':
        if not isinstance(voice, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', voice):
            raise ValueError('Fish 音色ID格式无效')
    elif (not isinstance(voice, str) or not voice.strip() or len(voice) > 256
          or voice != voice.strip() or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in voice)):
        raise ValueError('MiniMax 音色ID格式无效')
    return chosen


def metadata(settings):
    return {key:value for key,value in selection(settings).items() if key != 'label'}


def recorded_settings(settings, record):
    """Replay existing audio identity, even after the current selection changes."""
    provider = record.get('provider', 'fish')
    merged = {**settings, 'voice_provider':provider}
    if provider == 'fish':
        merged['voice_id'] = record.get('voice_id')
        from . import providers
        if record.get('model', providers.FISH_MODEL) != providers.FISH_MODEL:
            raise ValueError('已有整条配音的模型记录与当前接口不符；未调用收费接口')
    elif provider == 'minimax':
        merged.update(minimax_voice_id=record.get('voice_id'), minimax_model=record.get('model'))
    validate(merged)
    return merged


def synthesize(text, output, *, settings, api_key, cancel=None, commit_complete=False, adapters=None):
    from . import providers
    chosen = selection(settings)
    adapters = providers if adapters is None else adapters
    if chosen['provider'] == 'fish':
        # Omit the opt-in keyword for the historical segmented call contract.
        options = {'api_key':api_key, 'voice_id':chosen['voice_id'], 'cancel':cancel}
        if commit_complete:
            options['commit_complete'] = True
        return adapters.synthesize_voice(text, output, **options)
    return adapters.synthesize_minimax_voice(text, output, api_key=api_key,
        voice_id=chosen['voice_id'], model=chosen['model'], cancel=cancel,
        commit_complete=commit_complete)


def require_retry_selection(payload, settings):
    chosen = metadata(settings)
    previous = payload.get('voice')
    # Old jobs predate provider selection and always submitted to Fish.
    if previous is None:
        same = chosen['provider'] == 'fish'
    else:
        same = isinstance(previous, dict) and previous == chosen
    if not same:
        raise ValueError('配音服务、模型或音色已改变，请确认当前设置后重新生成；旧任务不会跨服务重试或自动扣费')


def require_confirmed_selection(expected, settings):
    if expected is not None and (not isinstance(expected, dict) or expected != metadata(settings)):
        raise ValueError('配音设置已变化，请重新确认当前服务、模型和音色后生成；尚未提交配音请求')
