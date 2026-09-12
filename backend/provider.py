import json
import os
import time
import httpx
from .db import dump,now
from .config import provider_for,model_for,require_external,validate_endpoint
from .oauth import authorization_headers

class ProviderError(ValueError):
    pass

class OpenRouter:
    def __init__(self,store,job_id,role):
        self.store,self.job_id,self.role=store,job_id,role
        self.settings=store.settings()
        self.provider=provider_for(self.settings)
        self.model=model_for(self.settings,role)
        previous=store.db.one('SELECT usage FROM jobs WHERE id=?',(job_id,))
        self.usage={'calls':0,'reserved_input_tokens':0,'reserved_output_tokens':0,'reported_input_tokens':0,'reported_output_tokens':0,**(json.loads(previous['usage']) if previous else {}),'provider':self.settings['api_provider'],'model':self.model}

    def generate(self,prompt,payload,schema):
        require_external(self.store)
        if not self.model:
            raise ProviderError(f'Configure a {self.provider["name"]} API key and model in Settings to enable AI processing.')
        validate_endpoint(self.provider['base_url'])
        schema_text=json.dumps(schema.model_json_schema())
        messages=[{'role':'system','content':prompt+'\nReturn only valid JSON conforming to this schema:\n'+schema_text},{'role':'user','content':dump(payload)}]
        # UTF-8 byte count is a deliberately conservative token upper bound.
        reserve_in=len(dump(messages).encode())+100
        reserve_out=min(4096,self.settings['max_output_tokens']-self.usage['reserved_output_tokens'])
        if reserve_in+reserve_out>self.settings['context_tokens']:
            raise ProviderError('This request exceeds the configured model context limit. Split the source or adjust context limits.')
        for attempt in range(3):
            require_external(self.store)
            if provider_for(self.store.settings()) != self.provider:
                raise ProviderError("Provider configuration changed. Retry the job using the new settings.")
            headers=authorization_headers(self.store,self.settings)
            reserve_out=min(reserve_out,self.settings['max_output_tokens']-self.usage['reserved_output_tokens'])
            if self.usage['calls']>=self.settings['max_calls'] or self.usage['reserved_input_tokens']+reserve_in>self.settings['max_input_tokens'] or reserve_out<256:
                raise ProviderError('Processing budget exhausted. No unvalidated knowledge was committed.')
            self.usage['calls']+=1
            self.usage['reserved_input_tokens']+=reserve_in
            self.usage['reserved_output_tokens']+=reserve_out
            self._record()
            try:
                require_external(self.store)
                if provider_for(self.store.settings()) != self.provider:
                    raise ProviderError('Provider configuration changed. Retry the job.')
                with httpx.Client(timeout=90,follow_redirects=False) as client:
                    response=client.post(self.provider['base_url']+'/chat/completions',headers={**headers,'X-Title':'Commonplace Wiki'},json={'model':self.model,'messages':messages,'response_format':{'type':'json_object'},'max_tokens':reserve_out,'temperature':0.1,**({'thinking':{'type':'disabled'}} if self.settings['api_provider'] in ('deepseek','kimi') else {'enable_thinking':False} if self.settings['api_provider']=='qwen' else {})})
                if response.status_code in (429,500,502,503,504) and attempt<2:
                    time.sleep(2**attempt)
                    continue
                if response.status_code!=200:
                    raise ProviderError(f'Provider returned HTTP {response.status_code}. Check the selected model, account access, and limits.')
                result=response.json()
                usage=result.get('usage',{})
                self.usage['reported_input_tokens']+=usage.get('prompt_tokens',0)
                self.usage['reported_output_tokens']+=usage.get('completion_tokens',0)
                if usage.get('cost') is not None:
                    self.usage['reported_cost']=self.usage.get('reported_cost',0)+usage['cost']
                self._record()
                text=result['choices'][0]['message']['content']
                return schema.model_validate_json(text)
            except (httpx.TimeoutException,httpx.ConnectError):
                if attempt==2:
                    raise ProviderError('Provider connection failed after bounded retries. A timed-out request may still have incurred a charge.')
            except (KeyError,IndexError,json.JSONDecodeError):
                raise ProviderError('Provider returned an invalid response')
        raise ProviderError('Provider did not return a usable response')

    def _record(self):
        self.store.db.execute('UPDATE jobs SET usage=?,updated=? WHERE id=?',(dump(self.usage),now(),self.job_id))
