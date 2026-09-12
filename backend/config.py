from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

def data_dir():
    p = Path(os.getenv("APP_DATA_DIR", "local-data")).expanduser()
    return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

DEFAULTS = {
    "api_provider": "openrouter",
    "external_models_enabled": False,
    "deepseek_model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
    "deepseek_compiler_model": "",
    "deepseek_research_model": "",
    "deepseek_linter_model": "",
    "default_model": os.getenv("DEFAULT_MODEL", ""),
    "compiler_model": os.getenv("COMPILER_MODEL", ""),
    "research_model": os.getenv("RESEARCH_MODEL", ""),
    "linter_model": os.getenv("LINTER_MODEL", ""),
    "review_first": False,
    "max_calls": 12,
    "max_input_tokens": 60000,
    "max_output_tokens": 16000,
    "context_tokens": 32000,
    "max_pages": 6,
    "max_text_chars": 100000,
    "max_upload_bytes": 20 * 1024 * 1024,
    "max_url_bytes": 10 * 1024 * 1024,
    "max_pdf_pages": 200,
    "large_page_chars": 20000,
    "stale_days": 365,
}

PROVIDERS = {
    "openrouter": {"name":"OpenRouter", "base_url":"https://openrouter.ai/api/v1", "key_env":"OPENROUTER_API_KEY"},
    "deepseek": {"name":"DeepSeek", "base_url":"https://api.deepseek.com", "key_env":"DEEPSEEK_API_KEY"},
}

for provider, model in [('qwen','qwen-plus'),('kimi','kimi-k2.5'),('openai',''),('grok',''),('custom','')]:
    DEFAULTS[provider+'_model']=model
    for role in ('compiler','research','linter'):
        DEFAULTS[provider+'_'+role+'_model']=''
DEFAULTS.update(custom_base_url='', custom_auth='api_key', qwen_base_url='https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
    oauth_authorize_url='', oauth_token_url='', oauth_client_id='', oauth_scope='', oauth_audience='')
PROVIDERS.update({
    'qwen': {'name':'Qwen', 'base_url':DEFAULTS['qwen_base_url'], 'key_env':'DASHSCOPE_API_KEY'},
    'kimi': {'name':'Kimi', 'base_url':'https://api.moonshot.ai/v1', 'key_env':'MOONSHOT_API_KEY'},
    'openai': {'name':'OpenAI / ChatGPT models', 'base_url':'https://api.openai.com/v1', 'key_env':'OPENAI_API_KEY'},
    'grok': {'name':'Grok', 'base_url':'https://api.x.ai/v1', 'key_env':'XAI_API_KEY'},
    'custom': {'name':'Custom OpenAI-compatible API', 'base_url':'', 'key_env':'CUSTOM_API_KEY'},
})

def provider_for(settings):
    key=settings['api_provider']
    provider=dict(PROVIDERS[key])
    if key in ('custom','qwen'):
        provider['base_url']=settings[key+'_base_url'].rstrip('/')
    return provider

def model_for(settings,role='compiler'):
    prefix='' if settings['api_provider']=='openrouter' else settings['api_provider']+'_'
    return settings.get(prefix+role+'_model') or settings['default_model' if not prefix else prefix+'model']

def require_external(store):
    if not store.settings()['external_models_enabled']:
        raise ValueError('External models are disabled. Enable external models in Settings before connecting or processing with AI.')

def validate_endpoint(value,allow_empty=False):
    from urllib.parse import urlsplit
    if allow_empty and not value:return
    u=urlsplit(value)
    local=u.hostname in ('localhost','127.0.0.1','::1')
    if not u.hostname or u.username or u.password or u.query or u.fragment or (u.scheme!='https' and not (u.scheme=='http' and local)) or any(c.isspace() for c in value):
        raise ValueError('Endpoint must be an HTTPS URL without credentials, query, or fragment. HTTP is allowed only for loopback servers.')
    try:u.port
    except ValueError:raise ValueError('Invalid endpoint port')
