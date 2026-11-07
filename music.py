"""Agenthood Music Studio workflow and marketplace integration."""
import fcntl
import io
import json
import logging
import os
import re
import shutil
import struct
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
import studio_audio as audio
import studio_song as song
from studio_references import production_cues
from studio_vocal import SpeechUnavailable, render_vocal
from pydantic import BaseModel, Field
from accounts import buyer
from storage import connect

router = APIRouter(prefix='/api/music', tags=['music studio'])
ROOT = Path(__file__).resolve().parent
OUTPUT = Path(os.getenv('AGENTHOOD_MUSIC_OUTPUT', str(ROOT / 'music_outputs')))
ROLES=['lyricist','composer','arranger','instrumentalist','singer','producer','marketing']
NAMES={
 'lyricist':['HookSmith','VerseCraft','NeonPen','Heartline'],
 'composer':['MelodyMint','ChordPilot','ToplineLab','HitTheory'],
 'arranger':['PulseArchitect','BuildDrop','HarmonyGrid','PopStructure'],
 'instrumentalist':['SynthKeys','BassPilot','RhythmForge','GuitarGlow'],
 'singer':['VoxGuide','VelvetSynth','ToplineVoice','NeonVox'],
 'producer':['MixCraft','LoudLab','StereoWorks','MasterPilot'],
 'marketing':['LaunchPulse','CoverCopy','SocialSpark','AudienceScout']
}
log = logging.getLogger(__name__)
BASE={'lyricist':.18,'composer':.26,'arranger':.24,'instrumentalist':.30,'singer':.36,'producer':.38,'marketing':.14}


def stamp(): return datetime.now(timezone.utc).isoformat()


def init_music():
 with connect() as c:
  c.executescript('''
  CREATE TABLE IF NOT EXISTS music_projects (id TEXT PRIMARY KEY, customer TEXT, brief TEXT, budget REAL, status TEXT, stage INTEGER DEFAULT 0, spent REAL DEFAULT 0, approved INTEGER DEFAULT 0, feedback TEXT DEFAULT '', created_at TEXT, updated_at TEXT, provider TEXT DEFAULT 'auto', song_name TEXT DEFAULT '', style TEXT DEFAULT '2010s-inspired pop');
  CREATE TABLE IF NOT EXISTS music_hires (id TEXT PRIMARY KEY, project_id TEXT, agent_id TEXT, role TEXT, price REAL, units INTEGER, status TEXT, output TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS music_events (id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, event TEXT, detail TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS music_reviews (id TEXT PRIMARY KEY, project_id TEXT, agent_id TEXT, rating INTEGER, comment TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS music_lessons (id TEXT PRIMARY KEY, agent_id TEXT, scope TEXT, content TEXT, created_at TEXT);
  ''')
  c.execute('BEGIN IMMEDIATE')
  columns = {r['name'] for r in c.execute('PRAGMA table_info(music_projects)')}
  if 'owner_id' not in columns:
   c.execute('ALTER TABLE music_projects ADD COLUMN owner_id TEXT REFERENCES users(id)')
  if 'allow_processing' not in columns:
   c.execute('ALTER TABLE music_projects ADD COLUMN allow_processing INTEGER NOT NULL DEFAULT 0')
  c.execute('''CREATE TABLE IF NOT EXISTS studio_customer_lessons (
    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), agent_id TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL)''')
  for role in ROLES:
   for i,name in enumerate(NAMES[role]):
    ident='music-'+role+'-'+str(i+1)
    c.execute('INSERT OR IGNORE INTO agents(id,name,ticker,skill,description,base_price,total_capacity,available_capacity,provider) VALUES(?,?,?,?,?,?,?,?,?)',
     (ident,name,role[:3].upper()+str(i+1),role,f'{role.title()} specialized in original pop production',round(BASE[role]*(.80+i*.22),3),36,36,'studio'))


def event(c,pid,name,details):
 c.execute('INSERT INTO music_events(project_id,event,detail,created_at) VALUES(?,?,?,?)',(pid,name,json.dumps(details),stamp()))


def quote(a):
 return round(a['base_price']*(1+.5*(1-a['available_capacity']/max(a['total_capacity'],1))),3)


def choose(c,role,remaining,pid):
 agents=[dict(x) for x in c.execute("SELECT * FROM agents WHERE skill=? AND provider='studio' AND available_capacity>=1",(role,))]
 if not agents: raise ValueError('No available '+role+' agents')
 # Aggregate ratings come from one completed-project review per hired specialist.
 rank=[]
 for a in agents:
  reviews=c.execute('SELECT COUNT(*) n,COALESCE(AVG(rating),0) rating FROM music_reviews WHERE agent_id=?',(a['id'],)).fetchone()
  # Bayesian smoothing avoids exaggerating brand-new workers' quality.
  quality=(reviews['rating']*reviews['n']+3.5*3)/(reviews['n']+3)
  score=quality*.45 - quote(a)*2.5
  rank.append((score,a))
 rank.sort(key=lambda x:x[0],reverse=True)
 for _,a in rank:
  if Decimal(str(quote(a)))<=remaining: return a
 raise ValueError('Remaining budget is too low for '+role)


def hire(c,pid,role,budget):
 a=choose(c,role,budget,pid)
 price=quote(a)
 n=c.execute('UPDATE agents SET available_capacity=available_capacity-1 WHERE id=? AND available_capacity>=1',(a['id'],)).rowcount
 if n!=1: raise ValueError('Capacity became unavailable')
 hid=uuid.uuid4().hex[:12]
 c.execute('INSERT INTO music_hires VALUES(?,?,?,?,?,?,?,?,?)',(hid,pid,a['id'],role,price,1,'working','',stamp()))
 c.execute('UPDATE music_projects SET spent=ROUND(spent+?,3),updated_at=? WHERE id=?',(price,stamp(),pid))
 event(c,pid,'agent_hired',{'role':role,'agent':a['name'],'ticker':a['ticker'],'price':price,'reason':'Bayesian review rating, price and available capacity'})
 return hid,a


def finish(c,pid,hid,a,summary,mode,files):
 c.execute('UPDATE music_hires SET status=?,output=? WHERE id=?',('completed',summary[:2000],hid))
 c.execute('UPDATE agents SET available_capacity=MIN(total_capacity,available_capacity+1),success=success+1 WHERE id=?',(a['id'],))
 event(c,pid,'agent_delivered',{'role':a['skill'],'agent':a['name'],'summary':summary[:250],'delivery':hid,'mode':mode,'files':files})


def path(pid,filename):
 if not re.fullmatch(r'[a-f0-9]{12}',pid): raise HTTPException(400,'Invalid ID')
 return OUTPUT / pid / filename


def midi_var(n):
 z=[n&127]; n>>=7
 while n: z.insert(0,(n&127)|128);n>>=7
 return bytes(z)


def make_midi(events, dest, tempo=112, ticks=480):
 # events: (beat, note, length, velocity, channel)
 bpm=int(60000000/tempo)
 track=bytearray(b'\x00\xff\x51\x03'+bpm.to_bytes(3,'big'))
 timeline=[]
 for b,n,d,v,ch in events:
  timeline.append((int(b*ticks),1,0x90|ch,n,v))
  timeline.append((int((b+d)*ticks),0,0x80|ch,n,0))
 last=0
 for at,_,cmd,n,v in sorted(timeline):
  track.extend(midi_var(max(0,at-last)));track.extend([cmd,n,v]);last=at
 track.extend(b'\x00\xff\x2f\x00')
 dest.write_bytes(b'MThd'+struct.pack('>IHHH',6,0,1,ticks)+b'MTrk'+struct.pack('>I',len(track))+track)


class NewMusic(BaseModel):
 customer:str=Field(min_length=2,max_length=100)
 brief:str=Field(min_length=10,max_length=1000,default='Create an original upbeat 2010s-inspired pop song with a soaring chorus.')
 budget:float=Field(default=5.0,ge=1,le=100)
 song_name:str=Field(default='Afterglow',min_length=2,max_length=50)
 provider:str=Field(default='auto')
 allow_processing:bool=False

class SongConsent(BaseModel):
 allow_hosted_song:bool=False


class Approval(SongConsent):
 approved:bool
 feedback:str=Field(default='',max_length=600)

class Rating(BaseModel):
 agent_id:str
 rating:int=Field(ge=1,le=5)
 comment:str=Field(default='',max_length=400)


def snapshot(pid):
 with connect() as c:
  c.execute('BEGIN')
  p=c.execute('SELECT * FROM music_projects WHERE id=?',(pid,)).fetchone()
  if not p: raise HTTPException(404,'Music project not found')
  hires=[dict(x) for x in c.execute('SELECT music_hires.*,agents.name,agents.ticker FROM music_hires JOIN agents ON agents.id=music_hires.agent_id WHERE project_id=? ORDER BY created_at',(pid,))]
  ev=[{**dict(x),'detail':json.loads(x['detail'])} for x in c.execute('SELECT * FROM music_events WHERE project_id=? ORDER BY id',(pid,))]
  artifacts = current_artifacts(c, pid)
  deliveries = {e['detail']['delivery']: e['detail'] for e in ev
                if e['event'] == 'agent_delivered' and 'delivery' in e['detail']}
  for h in hires:
   delivery = deliveries.get(h['id'], {})
   h.update(mode=delivery.get('mode'), files=delivery.get('files', []))
  return {'project':{k:p[k] for k in p.keys() if k != 'owner_id'},'hires':hires,'events':ev,'files':list(artifacts),'artifacts':artifacts,
          'song_model':song.configured(),'song_hosted':song.hosted()}

@router.get('/catalog')
def catalog():
 with connect() as c:
  agents=[dict(x) for x in c.execute('SELECT * FROM agents WHERE id LIKE "music-%" ORDER BY skill,base_price')]
  for a in agents:
   r=c.execute('SELECT COUNT(*) count,AVG(rating) avg FROM music_reviews WHERE agent_id=?',(a['id'],)).fetchone()
   a.update(quote=quote(a),rating_count=r['count'],rating_average=round(r['avg'],2) if r['avg'] is not None else None)
  return agents

@router.post('/projects')
def new_project(body:NewMusic, user=Depends(buyer)):
 if body.provider not in ('auto','demo','darkbloom','ollama'): raise HTTPException(400,'Unknown inference provider')
 if body.provider in ('darkbloom', 'ollama') and not body.allow_processing:
  raise HTTPException(422, 'Consent to sending the brief and feedback to the model provider')
 pid=uuid.uuid4().hex[:12]
 with connect() as c:
  c.execute('''INSERT INTO music_projects(id,customer,brief,budget,status,created_at,updated_at,provider,
            song_name,owner_id,allow_processing) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            (pid,body.customer,body.brief,body.budget,'created',stamp(),stamp(),body.provider,
             body.song_name,user['id'],body.allow_processing))
  event(c,pid,'studio_brief_received',{'customer':body.customer,'budget':body.budget,'song_name':body.song_name})
 return snapshot(pid)

@router.get('/projects')
def list_projects(user=Depends(buyer)):
 with connect() as c:
  return [dict(x) for x in c.execute('''SELECT id,song_name,status,stage,updated_at
          FROM music_projects WHERE owner_id=? ORDER BY created_at DESC LIMIT 30''', (user['id'],))]


def owned_project(pid: str, user=Depends(buyer)):
 with connect() as c:
  if not c.execute('SELECT 1 FROM music_projects WHERE id=? AND owner_id=?', (pid, user['id'])).fetchone():
   raise HTTPException(404, 'Music project not found')

@router.get('/projects/{pid}', dependencies=[Depends(owned_project)])
def project(pid:str): return snapshot(pid)

LEGACY_FILES = ('lyrics.txt', 'score.json', 'composition.mid', 'arrangement.txt',
                'instrumental.mid', 'vocal_melody.mid', 'final_mix.wav', 'marketing.md')
CHECKPOINTS = set(ROLES[:-1])
STEMS = ('drums.wav', 'bass.wav', 'keys.wav')
VOCAL = 'lyric_vocal.wav'
MODEL_TAKE = 'model_take.wav'
VOCAL_GUIDE = 'vocal_guide.wav'


def current_artifacts(c, pid):
    # Existing installations keep their flat files. New deliveries use immutable paths.
    artifacts = {name: None for name in LEGACY_FILES if path(pid, name).is_file()}
    for row in c.execute("SELECT detail FROM music_events WHERE project_id=? AND event='agent_delivered' ORDER BY id", (pid,)):
        delivery = json.loads(row['detail'])
        if 'delivery' in delivery:
            if VOCAL in delivery['files']:
                artifacts.pop(VOCAL_GUIDE, None)
            elif VOCAL_GUIDE in delivery['files']:
                artifacts.pop(VOCAL, None)
            artifacts.update(dict.fromkeys(delivery['files'], delivery['delivery']))
    return artifacts


def artifact_path(pid, artifacts, name):
    delivery = artifacts[name]
    return path(pid, f'deliveries/{delivery}/{name}' if delivery else name)


@contextmanager
def project_lock(pid):
    """An OS lock guards each stage across requests, threads and local workers."""
    lock = path(pid, '.studio.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(409, 'A specialist is already working on this project')
        yield


def fail_delivery(c, p, h, reason):
    c.execute("UPDATE music_hires SET status='failed',output=? WHERE id=?", (reason, h['id']))
    c.execute('UPDATE agents SET available_capacity=MIN(total_capacity,available_capacity+1),failures=failures+1 WHERE id=?',
              (h['agent_id'],))
    started = c.execute("SELECT detail FROM music_events WHERE project_id=? AND event='task_started' ORDER BY id DESC LIMIT 1",
                        (p['id'],)).fetchone()
    restore = json.loads(started[0]).get('restore_status') if started else None
    status = restore or ('awaiting_approval' if h['price'] == 0 else 'retryable')
    c.execute('UPDATE music_projects SET status=?,spent=ROUND(MAX(0,spent-?),3),updated_at=? WHERE id=?',
              (status, h['price'], stamp(), p['id']))
    event(c, p['id'], 'stage_error', {'role': h['role'], 'error': reason, 'refunded': h['price']})


def recover_interrupted(c, p):
    # The caller holds the project lock, so a running hire left here lost its worker.
    for h in c.execute("SELECT * FROM music_hires WHERE project_id=? AND status='working'", (p['id'],)).fetchall():
        fail_delivery(c, p, h, 'Interrupted by a server restart. Retry this stage.')
        shutil.rmtree(path(p['id'], f"deliveries/{h['id']}"), ignore_errors=True)


def recover_music():
    """Return capacity for interrupted work when the local server starts."""
    with connect() as c:
        running = [r[0] for r in c.execute("SELECT id FROM music_projects WHERE status='running'")]
    for pid in running:
        try:
            with project_lock(pid), connect() as c:
                c.execute('BEGIN IMMEDIATE')
                p = c.execute('SELECT * FROM music_projects WHERE id=?', (pid,)).fetchone()
                if p['status'] == 'running':
                    recover_interrupted(c, p)
        except HTTPException as exc:
            if exc.status_code != 409:
                raise


def reserve_revision(c, pid, role):
    agent = c.execute("""SELECT a.* FROM music_hires h JOIN agents a ON a.id=h.agent_id
        WHERE h.project_id=? AND h.role=? AND h.status='completed'
        ORDER BY h.created_at DESC LIMIT 1""", (pid, role)).fetchone()
    if not agent or not agent['available_capacity']:
        raise HTTPException(409, f'The original {role} is unavailable. Try again later.')
    c.execute('UPDATE agents SET available_capacity=available_capacity-1 WHERE id=?', (agent['id'],))
    hid = uuid.uuid4().hex[:12]
    c.execute('INSERT INTO music_hires VALUES(?,?,?,?,?,?,?,?,?)',
              (hid, pid, agent['id'], role, 0, 1, 'working', '', stamp()))
    return hid, dict(agent)


def start_delivery(pid, revision, feedback, refresh_vocals=False, allow_hosted_song=False):
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        p = c.execute('SELECT * FROM music_projects WHERE id=?', (pid,)).fetchone()
        if not p:
            raise HTTPException(404, 'Project not found')
        if p['status'] == 'running':
            recover_interrupted(c, p)
            p = c.execute('SELECT * FROM music_projects WHERE id=?', (pid,)).fetchone()
        # Commit recovery even when the requested action conflicts with the recovered state.
        c.commit()
    with connect() as c:
        c.execute('BEGIN IMMEDIATE')
        stage = p['stage']
        artifacts = current_artifacts(c, pid)
        remix = None
        next_stage = 4 if refresh_vocals else stage - 1 if revision else stage
        if next_stage == 4 and song.hosted() and not allow_hosted_song:
            raise HTTPException(422, 'Confirm sending the lyrics, brief, feedback and backing audio to the Tinfoil renderer.')
        if refresh_vocals:
            if stage < 5 or MODEL_TAKE in artifacts or (VOCAL in artifacts and not song.configured()):
                raise HTTPException(409, 'This project does not need a lyric-vocal upgrade')
            role = 'singer'
            hid, a = reserve_revision(c, pid, role)
            if stage >= 6:
                remix = reserve_revision(c, pid, 'producer')
            event(c, pid, 'lyric_vocal_upgrade_requested', {'remix': bool(remix)})
        elif revision:
            if p['status'] != 'awaiting_approval' or stage == 0:
                raise HTTPException(409, 'No delivery awaiting revision')
            role = ROLES[stage - 1]
            hid, a = reserve_revision(c, pid, role)
            event(c, pid, 'revision_requested', {'role': role, 'agent': a['name'], 'note': feedback})
        else:
            if p['status'] in ('awaiting_approval', 'complete') or stage >= len(ROLES):
                raise HTTPException(409, 'Approve the current delivery before advancing')
            role = ROLES[stage]
            try:
                hid, a = hire(c, pid, role, Decimal(str(p['budget'])) - Decimal(str(p['spent'])))
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        c.execute('UPDATE music_projects SET status=?,updated_at=? WHERE id=?', ('running', stamp(), pid))
        event(c, pid, 'task_started', {'role': role, 'agent': a['name'], 'delivery': hid,
                                      'restore_status': p['status'] if refresh_vocals else None})
        version = c.execute("""SELECT COUNT(*) FROM music_hires
            WHERE project_id=? AND role=? AND status='completed'""", (pid, role)).fetchone()[0]
        lessons = [r[0] for r in c.execute("""SELECT content FROM studio_customer_lessons
            WHERE agent_id=? AND owner_id=? ORDER BY created_at DESC LIMIT 3""", (a['id'], p['owner_id']))]
    return dict(p), dict(a), hid, role, artifacts, version, lessons, remix


def offline_lyrics(p, profile, version, feedback):
    theme = feedback or p['brief']
    if not feedback and not audio.has(theme, 'summer|beach|ocean|sunset|night|neon|city|dance|heartbreak|breakup|sad|melancholy'):
        cues = ' '.join(production_cues(p['brief']))
        theme += ' night' if 'dance-pop' in cues else ' melancholy' if 'moody' in cues else ''
    if audio.has(theme, 'summer|beach|ocean|sunset'):
        images = ('the tide rolls back', 'salt on the window', 'bare feet on the sand', 'the last light')
    elif audio.has(theme, 'night|neon|city|dance'):
        images = ('the streetlights blink', 'neon in the rain', 'the last downtown train', 'the blue light')
    elif audio.has(theme, 'heartbreak|breakup|sad|melancholy'):
        images = ('the quiet comes', 'your coat on the chair', 'the words we never said', 'the hall light')
    else:
        images = ('the sky breaks open', 'dust on the dashboard', 'a road without a name', 'the headlights')
    if (profile + version) % 2:
        verse = f'I left {images[1]} behind\nI carried {images[2]} in my mind\nIf {images[0]}, let it be\nThere is another side of me'
        refrain = f'{p["song_name"]}, turn the night around\nI want to hear how freedom sounds\nLet {images[3]} carry me home\nI can make this road my own'
    else:
        verse = f'When {images[0]}, I take the long way home\nPast {images[1]}, past all the things we know\nI keep {images[2]} like a photograph\nFold it in my pocket when the moment passes'
        refrain = f'{p["song_name"]}, I can feel it now\nEven when the whole room loses power\nHold on to {images[3]} till it goes\nWe can find a rhythm of our own'
    return f'{p["song_name"]}\n\n[Verse]\n{verse}\n\n[Build]\nOne more step and the ground gives way\nOne more breath and the sky turns gray\n\n[Chorus]\n{refrain}\n'


async def write_copy(p, a, role, dest, previous, profile, version, feedback, lessons, music_notes):
    provider = p['provider']
    if not p['allow_processing']:
        provider = 'demo'
    if provider == 'auto':
        if os.getenv('DARKBLOOM_BASE_URL') and os.getenv('DARKBLOOM_MODEL'):
            provider = 'darkbloom'
        else:
            provider = 'ollama' if os.getenv('OLLAMA_MODEL') else 'demo'
    lyricist = role == 'lyricist'
    if provider == 'demo':
        text = offline_lyrics(p, profile, version, feedback) if lyricist else (
            f'# {p["song_name"]} — release kit\n\n'
            f'Pitch: A short synthesized pop demo. {music_notes}\n\n'
            f'Caption: Hear how {p["song_name"]} came together, from the first hook to the final mix.\n\n'
            'Release checklist\n- Share a short chorus clip and a stem comparison.\n'
            '- Link the full demo and credit the studio team.\n- Invite feedback on the mix and hook.\n')
        mode = 'Offline deterministic template'
    else:
        from app import llm
        system = ('Write fresh original pop lyrics with a verse, build and memorable chorus.'
                  if lyricist else
                  'Write an original release pitch, three captions and a distribution checklist. '
                  'Describe the supplied vocal treatment accurately and avoid implying artist involvement.')
        system += (' Treat previous drafts and private buyer notes as reference material. Follow the creative brief and revision request. '
                   'Use production references only for broad traits. Write original material without quoting recordings or lyrics.')
        try:
            text = await llm(provider, system, json.dumps({
                'specialist': a['name'], 'title': p['song_name'], 'creative_brief': p['brief'],
                'revision_request': feedback, 'previous_draft': previous,
                'private_buyer_notes': lessons, 'music': music_notes,
                'production_cues': production_cues(p['brief']),
            }, ensure_ascii=False))
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f'{provider} did not return a draft. Check the provider configuration and retry.') from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f'{provider} returned an empty draft. Check the provider configuration and retry.')
        mode = f'Model generated · {provider}'
    (dest / ('lyrics.txt' if lyricist else 'marketing.md')).write_text(text.strip()[:9000] + '\n', encoding='utf-8')
    summary = 'Lyric draft with verse, build and chorus' if lyricist else 'Release pitch, captions and distribution checklist'
    return summary, mode


def deliver_audio(p, role, dest, inputs, profile, version, feedback, require_lyrics=False):
    cues = production_cues(p['brief'])
    brief = p['brief'] + (' · Production cues: ' + '; '.join(cues) if cues else '')

    def save_plan(name, plan):
        (dest / name).write_text(json.dumps(plan, indent=2), encoding='utf-8')

    def load_plan(name):
        return json.loads(inputs[name].read_text(encoding='utf-8'))

    def arrangement():
        if 'arrangement.json' in inputs:
            return load_plan('arrangement.json')
        score = load_plan('score.json')
        # Legacy projects used their score unchanged despite a text arrangement.
        return score

    def render_stems(folder, plan):
        for track, voice, name, gain, pan in (
            ('drums', 'drums', 'drums.wav', .53, 0),
            ('bass', 'bass', 'bass.wav', .34, 0),
            ('chords', 'keys', 'keys.wav', .13, -.30),
        ):
            audio.write_wav(folder / name, plan, [(track, voice, gain, pan)], plan['timbre'])

    if role == 'composer':
        previous = load_plan('score.json') if version else None
        score = audio.compose(brief, p['song_name'], profile, feedback, version, previous)
        save_plan('score.json', score)
        make_midi(score['lead'] + score['chords'] + score['bass'], dest / 'composition.mid', score['tempo'])
        audio.write_wav(dest / 'melody.wav', score, [('lead', 'lead', .31, .13)])
        audio.write_wav(dest / 'composition.wav', score, [('lead', 'lead', .28, .16),
                       ('chords', 'keys', .12, -.24), ('bass', 'bass', .18, 0)], profile)
        return f"Eight-bar melody and chords at {score['tempo']} BPM in {score.get('mode', 'major')}"
    if role == 'arranger':
        plan = audio.arrange(load_plan('score.json'), brief, profile, feedback, version)
        save_plan('arrangement.json', plan)
        sections = [f"{s['name']} · bars {s['start'] + 1}–{s['start'] + s['bars']}" for s in plan['sections']]
        (dest / 'arrangement.txt').write_text('\n'.join(sections) + '\n', encoding='utf-8')
        audio.write_wav(dest / 'arrangement.wav', plan, [('lead', 'lead', .26, .19),
                       ('chords', 'keys', .11, -.28), ('bass', 'bass', .20, 0)], profile)
        return f"{plan['bars']}-bar structure · " + ' / '.join(s['name'] for s in plan['sections'])
    if role == 'instrumentalist':
        plan = audio.perform(arrangement(), brief, profile, feedback, version)
        save_plan('performance.json', plan)
        make_midi(plan['chords'] + plan['bass'] + plan['drums'], dest / 'instrumental.mid', plan['tempo'])
        render_stems(dest, plan)
        audio.mix_wavs(dest / 'instrumental.wav', [(dest / name, .85) for name in STEMS])
        return 'Performed and rendered separate drums, bass and synth keys'
    if role == 'singer':
        plan = audio.guide(arrangement(), profile, feedback, version)
        make_midi(plan['lead'], dest / 'vocal_melody.mid', plan['tempo'])
        vocal = dest / VOCAL
        try:
            lines = render_vocal(vocal, plan, inputs['lyrics.txt'].read_text(encoding='utf-8'),
                                 p['song_name'], audio.has(feedback, 'smoother|legato'))
            summary = f'Performed {lines} lyric phrases over the melody'
        except SpeechUnavailable:
            if require_lyrics:
                raise
            vocal.unlink(missing_ok=True)
            vocal = dest / VOCAL_GUIDE
            audio.write_wav(vocal, plan, [('lead', 'lead', .43, 0)])
            summary = 'Rendered a wordless melody guide because the speech engine was unavailable'
        instrumental = inputs.get('instrumental.wav')
        if not instrumental:
            # Carry projects created before WAV stems existed into the new mix workflow.
            render_stems(dest, audio.perform(arrangement(), brief, profile))
            instrumental = dest / 'instrumental.wav'
            audio.mix_wavs(instrumental, [(dest / name, .85) for name in STEMS])
        audio.mix_wavs(dest / 'pre_mix.wav', [(instrumental, .76), (vocal, .75)])
        return summary + ' and rendered a full pre-mix'
    if role == 'producer':
        if MODEL_TAKE in inputs:
            seconds = song.master_take(inputs[MODEL_TAKE], dest / 'final_mix.wav', feedback)
            return f'Mastered the approved model performance · {seconds:.1f}s'
        vocal = inputs.get(VOCAL) or inputs.get(VOCAL_GUIDE)
        if not vocal:
            raise ValueError('Add the lyric vocal in the control room before mixing this older project.')
        if not all(name in inputs for name in STEMS):
            render_stems(dest, audio.perform(arrangement(), brief, profile))
        default_gain = .79 if audio.has(brief, 'upfront vocal') else .70
        vocal_gain = .48 if audio.has(feedback, 'quieter vocal|softer vocal') else .96 if audio.has(feedback, 'louder vocal') else default_gain
        bass_gain = 1.05 if audio.has(feedback, 'more bass|heavier bass') else .71
        sources = [(inputs.get(name, dest / name), gain) for name, gain in
                   zip(STEMS, (.70 + profile * .04, bass_gain, .68))]
        sources.append((vocal, vocal_gain))
        width = 1.45 if audio.has(feedback, 'wider') else 1.10 + ((profile + version) % 4) * .09
        seconds = audio.mix_wavs(dest / 'final_mix.wav', sources, master=True, width=width)
        return f'Mixed the delivered stems with stereo balance and peak control · {seconds:.1f}s'
    raise ValueError('Unknown audio role')


async def deliver_song(p, dest, inputs, feedback, progress):
    score = inputs.get('arrangement.json') or inputs['score.json']
    plan = json.loads(score.read_text(encoding='utf-8'))
    backing = inputs.get('instrumental.wav')
    if backing is None:
        # Very old projects saved MIDI but had no backing WAV to condition on.
        backing = dest / 'conditioning_sketch.wav'
        await run_in_threadpool(audio.write_wav, backing, plan,
                                [('lead', 'lead', .15, .12), ('chords', 'keys', .12, -.2), ('bass', 'bass', .24, 0)])
    lyrics = inputs['lyrics.txt'].read_text(encoding='utf-8')
    request = song.render_request(p, plan, lyrics, feedback)
    result = await song.generate_take(request, backing, progress)
    (dest / MODEL_TAKE).write_bytes(result)
    (dest / 'song_render.json').write_text(json.dumps(request, indent=2), encoding='utf-8')
    return 'Generated a sung performance and backing from the approved lyrics and instrumental sketch'


async def execute_delivery(pid, revision=False, feedback='', refresh_vocals=False, allow_hosted_song=False):
    with project_lock(pid):
        p, a, hid, role, artifacts, version, lessons, remix = start_delivery(pid, revision, feedback, refresh_vocals, allow_hosted_song)
        dest = path(pid, f'deliveries/{hid}')
        remix_dir = path(pid, f'deliveries/{remix[0]}') if remix else None
        try:
            dest.mkdir(parents=True)
            inputs = {name: artifact_path(pid, artifacts, name) for name in artifacts}
            profile = int(a['id'].rsplit('-', 1)[-1]) - 1
            if role in ('lyricist', 'marketing'):
                previous = inputs['lyrics.txt'].read_text(encoding='utf-8')[:9000] if 'lyrics.txt' in inputs else ''
                notes = inputs['arrangement.txt'].read_text(encoding='utf-8') if 'arrangement.txt' in inputs else ''
                if role == 'marketing':
                    treatment = 'legacy wordless guide'
                    if MODEL_TAKE in inputs:
                        treatment = 'ACE-Step model-generated sung performance'
                    elif VOCAL in inputs:
                        treatment = 'synthesized lyric vocal with a MIDI-tuned vocoder'
                    elif VOCAL_GUIDE in inputs:
                        treatment = 'synthesized wordless melody guide'
                    notes += f'\nVocal treatment: {treatment}.'
                summary, mode = await write_copy(p, a, role, dest, previous, profile, version, feedback, lessons, notes)
            elif role == 'singer' and (song.configured() or MODEL_TAKE in inputs):
                def progress(job_id):
                    with connect() as c:
                        event(c, pid, 'song_model_submitted', {'job': job_id, 'delivery': hid,
                                                               'hosting': 'tinfoil' if song.hosted() else 'local'})
                        c.execute('UPDATE music_projects SET updated_at=? WHERE id=?', (stamp(), pid))
                summary = await deliver_song(p, dest, inputs, feedback, progress)
                mode = 'Model sung performance · ACE-Step' + (' on Tinfoil' if song.hosted() else '')
            else:
                summary = await run_in_threadpool(deliver_audio, p, role, dest, inputs, profile,
                                                  version, feedback, require_lyrics=refresh_vocals)
                mode = 'Deterministic audio synthesis'
                if role == 'singer':
                    mode = ('Synthesized lyric vocal · macOS speech and MIDI vocoder'
                            if (dest / VOCAL).is_file() else 'Algorithmic fallback · wordless melody guide')
            files = sorted(f.name for f in dest.iterdir() if f.is_file() and f.stat().st_size > 0)
            if not files:
                raise RuntimeError('Delivery did not produce artifacts')
            if remix:
                remix_id, producer = remix
                remix_dir.mkdir(parents=True)
                with connect() as c:
                    event(c, pid, 'task_started', {'role': 'producer', 'agent': producer['name'],
                          'delivery': remix_id, 'restore_status': p['status']})
                updated_inputs = {**inputs, **{name: dest / name for name in files}}
                remix_summary = await run_in_threadpool(deliver_audio, p, 'producer', remix_dir, updated_inputs,
                                                       int(producer['id'].rsplit('-', 1)[-1]) - 1, 1, '')
                remix_files = sorted(f.name for f in remix_dir.iterdir())
            stage = p['stage'] if revision or refresh_vocals else p['stage'] + 1
            status = 'awaiting_approval' if role in CHECKPOINTS else 'complete'
            if refresh_vocals and p['status'] == 'complete':
                status = 'complete'
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                finish(c, pid, hid, a, summary, mode, files)
                if remix:
                    finish(c, pid, remix_id, producer, remix_summary, 'Deterministic audio synthesis', remix_files)
                if mode.startswith('Model generated'):
                    event(c, pid, 'real_model_inference', {'role': role, 'provider': mode.split(' · ')[1],
                                                          'agent': a['name'], 'delivery': hid})
                event(c, pid, 'lyric_vocal_upgrade_delivered' if refresh_vocals else
                      'approval_requested' if status == 'awaiting_approval' else 'project_completed',
                      {'role': role, 'delivery': hid, 'revision': revision})
                c.execute('UPDATE music_projects SET stage=?,status=?,feedback=?,updated_at=? WHERE id=?',
                          (stage, status, feedback, stamp(), pid))
        except BaseException as exc:
            reason = str(exc) if isinstance(exc, ValueError) else 'Specialist execution failed. Retry this stage.'
            log.warning('Studio delivery %s (%s) failed: %s', hid, role, reason)
            with connect() as c:
                c.execute('BEGIN IMMEDIATE')
                h = c.execute('SELECT * FROM music_hires WHERE id=?', (hid,)).fetchone()
                fail_delivery(c, p, h, reason)
                if remix:
                    producer_hire = c.execute('SELECT * FROM music_hires WHERE id=?', (remix[0],)).fetchone()
                    fail_delivery(c, p, producer_hire, reason)
            shutil.rmtree(dest, ignore_errors=True)
            if remix_dir:
                shutil.rmtree(remix_dir, ignore_errors=True)
            if not isinstance(exc, Exception):
                raise
            raise HTTPException(422, reason) from exc
    return snapshot(pid)


@router.post('/projects/{pid}/advance', dependencies=[Depends(owned_project)])
async def advance(pid: str, consent: SongConsent | None = None):
    return await execute_delivery(pid, allow_hosted_song=bool(consent and consent.allow_hosted_song))


@router.post('/projects/{pid}/lyric-vocals', dependencies=[Depends(owned_project)])
async def refresh_lyric_vocals(pid: str, consent: SongConsent | None = None):
    return await execute_delivery(pid, refresh_vocals=True, allow_hosted_song=bool(consent and consent.allow_hosted_song))


@router.post('/projects/{pid}/approve', dependencies=[Depends(owned_project)])
async def approve(pid: str, data: Approval):
    if not data.approved:
        return await execute_delivery(pid, revision=True, feedback=data.feedback, allow_hosted_song=data.allow_hosted_song)
    with project_lock(pid):
        with connect() as c:
            c.execute('BEGIN IMMEDIATE')
            p = c.execute('SELECT * FROM music_projects WHERE id=?', (pid,)).fetchone()
            if not p:
                raise HTTPException(404, 'Project not found')
            if p['status'] != 'awaiting_approval':
                raise HTTPException(409, 'No approval pending')
            c.execute('UPDATE music_projects SET status=?,updated_at=? WHERE id=?', ('working', stamp(), pid))
            event(c, pid, 'approval_granted', {'role': ROLES[p['stage'] - 1], 'note': data.feedback})
    return snapshot(pid)


@router.get('/projects/{pid}/files/{filename}', dependencies=[Depends(owned_project)])
def download(pid: str, filename: str):
    with connect() as c:
        if not c.execute('SELECT 1 FROM music_projects WHERE id=?', (pid,)).fetchone():
            raise HTTPException(404, 'Project not found')
        artifacts = current_artifacts(c, pid)
    if filename == 'bundle.zip' and artifacts:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
            for name in artifacts:
                bundle.write(artifact_path(pid, artifacts, name), name)
        return Response(buffer.getvalue(), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="bundle.zip"'})
    if filename not in artifacts:
        raise HTTPException(404, 'File not produced yet')
    return delivery_response(artifact_path(pid, artifacts, filename), filename)


@router.get('/projects/{pid}/deliveries/{hid}/{filename}', dependencies=[Depends(owned_project)])
def download_delivery(pid: str, hid: str, filename: str):
    with connect() as c:
        deliveries = c.execute("SELECT detail FROM music_events WHERE project_id=? AND event='agent_delivered'", (pid,))
        for row in deliveries:
            detail = json.loads(row['detail'])
            if detail.get('delivery') == hid and filename in detail['files']:
                return delivery_response(path(pid, f'deliveries/{hid}/{filename}'), filename)
    raise HTTPException(404, 'File not produced yet')


def delivery_response(target, filename):
    if not target.is_file():
        raise HTTPException(404, 'File not produced yet')
    return FileResponse(target, filename=filename, content_disposition_type='inline',
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, no-store'})


@router.post('/projects/{pid}/review', dependencies=[Depends(owned_project)])
async def review(pid:str,data:Rating):
 with connect() as c:
  c.execute('BEGIN IMMEDIATE')
  p=c.execute('SELECT * FROM music_projects WHERE id=?',(pid,)).fetchone()
  if not p: raise HTTPException(404,'Project not found')
  if p['status']!='complete': raise HTTPException(409,'Rate agents after the project completes')
  hired=c.execute("SELECT 1 FROM music_hires WHERE project_id=? AND agent_id=? AND status='completed'",(pid,data.agent_id)).fetchone()
  if not hired: raise HTTPException(400,'Agent was not hired for this project')
  prior=c.execute('SELECT id FROM music_reviews WHERE project_id=? AND agent_id=?',(pid,data.agent_id)).fetchone()
  if prior: raise HTTPException(409,'This agent was already reviewed for this project')
  c.execute('INSERT INTO music_reviews VALUES(?,?,?,?,?,?)',(uuid.uuid4().hex[:12],pid,data.agent_id,data.rating,data.comment,stamp()))
  lesson=f'Public rating for {data.agent_id}: {data.rating}/5.'
  if data.comment.strip():
   c.execute('INSERT INTO studio_customer_lessons VALUES(?,?,?,?,?)',
             (uuid.uuid4().hex[:12],p['owner_id'],data.agent_id,data.comment,stamp()))
  # Public reputation moves listed service price by up to 4% per unique completed order review.
  # This is a posted service quote, never a financial security or speculative share price.
  multiplier=1+max(-.04,min(.04,(data.rating-3)*.02))
  c.execute('UPDATE agents SET base_price=ROUND(MAX(0.05,MIN(3.0,base_price*?)),4) WHERE id=?',(multiplier,data.agent_id))
  event(c,pid,'reputation_repriced',{'agent_id':data.agent_id,'multiplier':multiplier,'rating':data.rating})
  event(c,pid,'public_review_saved',{'agent_id':data.agent_id,'rating':data.rating,'note':data.comment,'market_impact':'review affects future hiring rank and the posted base price'})
 # Optional Cognee ingestion; errors must be visible and must not invalidate the review.
 try:
  from app import cognee_push
  sync=await cognee_push(lesson)
 except Exception as e: sync='error: '+str(e)[:150]
 with connect() as c:event(c,pid,'review_memory_sync',{'cognee_status':sync})
 return {'review_saved':True,'cognee':sync,'project':snapshot(pid)}

@router.get('/reviews')
def reviews():
 with connect() as c:
  return [dict(r) for r in c.execute('''SELECT a.id AS agent_id,a.name,COUNT(*) AS count,AVG(r.rating) AS average
          FROM music_reviews r JOIN agents a ON r.agent_id=a.id GROUP BY a.id ORDER BY a.name''')]
