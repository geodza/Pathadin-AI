import asyncio
import base64
import json
import os
import time
from urllib.parse import quote
import httpx
from .protocol import canonical, Read

KEYS = {'openai':'OPENAI_API_KEY','anthropic':'ANTHROPIC_API_KEY','gemini':'GEMINI_API_KEY'}

def configured():
    return {p:{'configured':bool(os.environ.get(k)),
               'models':[s.strip() for s in os.environ.get(p.upper()+'_MODELS','').split(',') if s.strip()]}
            for p,k in KEYS.items()}

def response_schema(user):
    schema = Read.model_json_schema()
    try:
        task = json.loads(user)
    except (ValueError,TypeError):
        return schema
    if not isinstance(task,dict): return schema
    if task.get('stage') == 'batch':
        ids = sorted(set(task['tile_ids']))
    elif task.get('stage') == 'synthesis':
        ids = sorted({tid for report in task['reports'] for region in report['regions'] for tid in region['tile_ids']})
    else: return schema
    fields = [schema['properties']['assessed_tile_ids'],
              schema['$defs']['Region']['properties']['tile_ids'],
              schema['$defs']['Followup']['properties']['target_tile_ids']]
    if ids and len(ids) <= 900:
        # Share the enum rather than repeating it across all three citation fields.
        schema['$defs']['EvidenceTileId'] = {'type':'string','enum':ids}
        for field in fields: field['items'] = {'$ref':'#/$defs/EvidenceTileId'}
    elif not ids:
        schema['properties']['regions']['maxItems'] = 0
        fields[0]['maxItems'] = fields[2]['maxItems'] = 0
    # Large synthesis sets still undergo the mandatory local citation validation.
    return schema

def payload(provider,model,system,user,images,max_tokens):
    """Translate transport only. Image bytes/order and prompt text are unchanged."""
    blocks = []
    for tid,data in images:
        b64 = base64.b64encode(data).decode('ascii')
        if provider == 'openai':
            blocks.extend([{'type':'input_text','text':'Image ID: '+tid},
                {'type':'input_image','image_url':'data:image/png;base64,'+b64,'detail':'high'}])
        elif provider == 'anthropic':
            blocks.extend([{'type':'text','text':'Image ID: '+tid},
                {'type':'image','source':{'type':'base64','media_type':'image/png','data':b64}}])
        elif provider == 'gemini':
            blocks.extend([{'text':'Image ID: '+tid}, {'inlineData':{'mimeType':'image/png','data':b64}}])
    if provider == 'openai':
        blocks.append({'type':'input_text','text':user})
        return 'https://api.openai.com/v1/responses', {'Authorization':'Bearer '+os.environ.get(KEYS[provider],'')}, {
            'model':model,'instructions':system,'input':[{'role':'user','content':blocks}],
            'max_output_tokens':max_tokens,'store':False,
            'text':{'format':{'type':'json_schema','name':'pathology_read',
                              'strict':True,'schema':response_schema(user)}}}
    if provider == 'anthropic':
        blocks.append({'type':'text','text':user})
        return 'https://api.anthropic.com/v1/messages', {'x-api-key':os.environ.get(KEYS[provider],''),
            'anthropic-version':'2023-06-01'}, {'model':model,'system':system,
            'messages':[{'role':'user','content':blocks}],'max_tokens':max_tokens}
    if provider == 'gemini':
        blocks.append({'text':user})
        return 'https://generativelanguage.googleapis.com/v1beta/models/'+quote(model,safe='')+':generateContent', {
            'x-goog-api-key':os.environ.get(KEYS[provider],'')}, {
            'systemInstruction':{'parts':[{'text':system}]}, 'contents':[{'role':'user','parts':blocks}],
            'generationConfig':{'maxOutputTokens':max_tokens}}
    raise ValueError('Unsupported provider')

def extract(provider,data):
    if provider == 'openai':
        text = ''.join(part.get('text','') for item in data.get('output',[]) for part in item.get('content',[]) if part.get('type') == 'output_text')
        finish = data.get('status')
        if finish == 'incomplete': raise ValueError('Provider returned incomplete output')
        return text,data.get('usage',{}),data.get('model'),data.get('id'),finish
    if provider == 'anthropic':
        if data.get('stop_reason') == 'max_tokens': raise ValueError('Provider output token limit reached')
        return ''.join(b.get('text','') for b in data.get('content',[]) if b.get('type') == 'text'),data.get('usage',{}),data.get('model'),data.get('id'),data.get('stop_reason')
    candidate = (data.get('candidates') or [{}])[0]
    if candidate.get('finishReason') not in {None,'STOP'}: raise ValueError('Provider finish reason: '+str(candidate.get('finishReason')))
    return ''.join(p.get('text','') for p in candidate.get('content',{}).get('parts',[]) if not p.get('thought')),data.get('usageMetadata',{}),data.get('modelVersion'),data.get('responseId'),candidate.get('finishReason')

def demo_read(user):
    task = __import__('json').loads(user)
    ids = task.get('tile_ids',[])
    if not ids:
        ids = list(dict.fromkeys(t for r in task.get('reports',[]) for region in r['regions'] for t in region['tile_ids']))
    return canonical({'assessed_tile_ids':task.get('tile_ids',[]),
        'regions':[{'tile_ids':ids[:1],'role':'uncertain','morphology':'Synthetic colored shapes. This fixture demonstrates coordinate linking only.'}] if ids else [],
        'primary_dx':'insufficient_evidence','grade':None,'differential':[],
        'explanation':'SIMULATED OUTPUT. No language model was called and no diagnosis was performed.',
        'requested_followups':[], 'count_based_estimates':[], 'confidence':0.0,
        'limitations':['Synthetic demo; excluded from research comparisons.']})

async def call(provider, model, system, user, images, max_tokens, cancelled, transport=None, on_attempt=None):
    start = time.monotonic()
    if provider == 'demo':
        if on_attempt: on_attempt()
        await asyncio.sleep(0.08)
        return {'raw':demo_read(user),'usage':{},'resolved_model':'synthetic-fixture-v1','request_id':None,
                'finish':'simulated','latency_s':round(time.monotonic()-start,3),'attempts':1}
    if not os.environ.get(KEYS[provider]): raise ValueError('API key is not configured for '+provider)
    url,headers,body = payload(provider,model,system,user,images,max_tokens)
    if len(canonical(body).encode()) > 20_000_000: raise ValueError('Request exceeds local 20 MB payload guard')
    async with httpx.AsyncClient(timeout=httpx.Timeout(180,connect=20),transport=transport) as client:
        for attempt in range(4):
            if cancelled(): raise InterruptedError('Cancelled before API request')
            if on_attempt: on_attempt()
            delay = [5,10,20,20][attempt]
            try:
                response = await client.post(url,headers=headers,json=body)
                if response.status_code in {429,500,502,503,504,529}:
                    if attempt == 3: raise ValueError(f'{provider}: HTTP {response.status_code} after 4 attempts')
                    try: delay = min(60,max(delay,float(response.headers.get('retry-after',0))))
                    except ValueError: pass
                elif response.is_error:
                    # Keep bounded error body, and redact the API key if an upstream server echoed it.
                    message = response.text[:1000].replace(os.environ[KEYS[provider]],'[REDACTED]')
                    raise ValueError(f'{provider}: HTTP {response.status_code}: {message}')
                else:
                    data = response.json()
                    try: text,usage,resolved,rid,finish = extract(provider,data)
                    except ValueError as exc:
                        return {'raw':canonical(data),'usage':data.get('usage',data.get('usageMetadata',{})),
                                'error':str(exc),'latency_s':round(time.monotonic()-start,3),'attempts':attempt+1}
                    return {'raw':text,'usage':usage,'resolved_model':resolved,'request_id':rid,'finish':finish,
                            'latency_s':round(time.monotonic()-start,3),'attempts':attempt+1}
            except (httpx.TimeoutException,httpx.NetworkError) as exc:
                if attempt == 3: raise ValueError(f'{provider}: network/timeout error after 4 attempts') from exc
            for _ in range(int(delay*10)):
                if cancelled(): raise InterruptedError('Cancelled during retry delay')
                await asyncio.sleep(0.1)
