/* Narration drives this page only. Pause freezes presentation, never an already submitted job. */
(() => {
  const visitor = location.pathname === '/demo/visitor';
  const api = visitor ? '/api/demo-cluster' : '/api/demo';
  const $ = s => document.querySelector(s);
  const style = document.createElement('style');
  style.textContent = '.narration-panel{border-left:2px solid var(--mint);padding:8px 12px;margin:10px 0;color:var(--muted);font-size:13px}.narration-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.narration-panel audio{height:30px;max-width:230px}.narration-panel p{margin:8px 0}.narration-panel summary{cursor:pointer}.narration-panel li{padding:5px 0}.narration-panel .quiet{padding:5px 9px}';
  document.head.append(style);
  const panel = document.createElement('section');
  panel.className = 'narration-panel';
  panel.innerHTML = '<div class="narration-controls"><button class="quiet" id="autorun">▶ Auto-run this demo</button><button class="quiet" id="pause-autorun" disabled>Pause</button><button class="quiet" id="stop-autorun" disabled>Stop</button><label><input id="narration-enabled" type="checkbox" checked> Audio narration</label><audio id="narration-audio" controls preload="auto" aria-label="Demo narration"></audio></div><p id="narration-note" role="status">Narration advances this case and grants its demo approvals. Pause or stop at any time. Reset each case separately.</p><details id="playbook"><summary>Presenter notes · play any section</summary><ol id="playbook-notes"></ol><small>Synthetic voice generated locally with Piper / LJ Speech. Audio is included with this site.</small></details>';
  const inspectionTools = $('.inspect-tools');
  inspectionTools.after(panel);
  $('.narration-controls').append($('#inspect-trust'));
  inspectionTools.remove();
  const audio = $('#narration-audio');
  let notes=[], active=false, paused=false, epoch=0, currentId=null, audioResolve=null;
  let runDeadline=0, pausedAt=0, pausedTotal=0, currentNote='';
  const delay = ms => new Promise(resolve=>setTimeout(resolve,ms));
  const tell = text => $('#narration-note').textContent=text;
  const current = ticket => active && ticket===epoch;
  const clock = () => Date.now()-pausedTotal-(paused ? Date.now()-pausedAt : 0);
  async function gate(ticket) {
    while(current(ticket) && paused) await delay(100);
    return current(ticket);
  }
  async function snapshot() {
    const response=await fetch(api);
    if(!response.ok) throw Error('Stage unavailable.');
    return response.json();
  }
  function silence() {
    audio.pause();
    if(audioResolve) audioResolve();
  }
  function controls() {
    $('#autorun').disabled=active;
    $('#pause-autorun').disabled=$('#stop-autorun').disabled=!active;
    $('#pause-autorun').textContent=paused?'Resume':'Pause';
    $('.actions').inert=active;
    if($('.input-choice')) $('.input-choice').inert=active;
    $('#playbook-notes').inert=active;
  }
  function setPaused(value) {
    if(!active || paused===value) return;
    if(value) pausedAt=Date.now();
    else pausedTotal+=Date.now()-pausedAt;
    paused=value;
    window.demoControl.pauseDisplay(value);
    if(value) {
      audio.pause();
      tell('Paused · '+currentNote+' Any submitted compute continues; later demo actions wait.');
    } else {
      tell(currentNote);
      if(audioResolve && $('#narration-enabled').checked) audio.play().catch(()=>setPaused(true));
    }
    controls();
  }
  function stop(message) {
    active=false;epoch++;paused=false;
    silence();
    window.demoControl.pauseDisplay(false);
    controls();
    if(message) tell(message);
  }
  async function speak(note,ticket) {
    if(ticket!==undefined && !await gate(ticket)) return;
    silence();currentNote=note.text;tell(note.text);
    audio.src='/demo-audio/'+note.id+'.mp3';audio.load();
    if(!$('#narration-enabled').checked) {await delay(800);return;}
    await new Promise(resolve=>{
      let done=false;
      const finish=()=>{
        if(done) return;
        done=true;audio.onended=null;audio.onerror=null;audioResolve=null;resolve();
      };
      audioResolve=finish;
      audio.onended=finish;
      audio.onerror=()=>{
        if(active) setPaused(true);
        tell(note.text+' Audio could not load. Turn off Audio narration and resume to continue with notes.');
        finish();
      };
      audio.play().catch(()=>{
        if(active) setPaused(true);
        tell(note.text+' Playback is blocked. Press Resume or the audio play control to continue in sync.');
      });
    });
  }
  // Native audio controls pause/resume the orchestration too, rather than allowing it to run ahead.
  audio.addEventListener('pause',()=>{
    if(active && audioResolve && audio.paused && !audio.ended && audio.currentTime>0) setPaused(true);
  });
  audio.addEventListener('play',()=>{if(active && paused) setPaused(false);});
  const initialized=fetch('/demo-audio/playbook.json').then(response=>{
    if(!response.ok) throw Error('Narration notes unavailable.');
    return response.json();
  }).then(data=>{
    notes=data[visitor?'visitor':'cohorts'];notes.error=data.common[0];
    for(const note of notes) {
      const li=document.createElement('li'),button=document.createElement('button');
      button.className='quiet';button.textContent='Play';
      button.onclick=()=>{if(!active) speak(note);};
      li.append(button,' '+note.text);$('#playbook-notes').append(li);
    }
    audio.src='/demo-audio/'+notes[0].id+'.mp3';audio.load();
  }).catch(error=>{tell(error.message);return false;});
  $('#pause-autorun').onclick=()=>setPaused(!paused);
  $('#stop-autorun').onclick=()=>stop('Auto-run and narration stopped. Any submitted computation continues; no later approval or export step will be triggered.');
  $('#narration-enabled').onchange=()=>{if(!$('#narration-enabled').checked) silence();};
  $('#reset').addEventListener('click',()=>stop('Reset applies only to this demo. The other demo is unchanged.'),{capture:true});
  window.addEventListener('pagehide',()=>stop());
  async function waitFor(predicate,ticket,timeout=240000) {
    const deadline=Math.min(clock()+timeout,runDeadline);
    while(await gate(ticket)) {
      if(clock()>=deadline) throw Error('Still waiting for the workflow. Auto-run stopped; the live job and its status remain available.');
      const {run}=await snapshot();
      if(!await gate(ticket)) return null;
      if(!run || run.id!==currentId) throw Error('This run was reset or replaced.');
      if(run.state==='failed') throw Error(run.events.at(-1)?.text||'Analysis failed.');
      if(predicate(run)) return run;
      await delay(650);
    }
    return null;
  }
  async function step(action,ticket) {
    if(!await gate(ticket)) return false;
    await window.demoControl.act(action);
    return gate(ticket);
  }
  $('#autorun').onclick=async()=>{
    if(active) return;
    active=true;paused=false;pausedTotal=0;runDeadline=clock()+150000;
    const ticket=++epoch;controls();
    try {
      await initialized;
      if(!notes.length) throw Error('Narration notes unavailable.');
      if(!await gate(ticket)) return;
      // Playback is activated by this user gesture. Narration and work are phase-aligned.
      const intro=speak(notes[0],ticket);
      const initial=await snapshot();
      if(!await gate(ticket)) return;
      if(initial.run) throw Error('Reset this demo before starting its auto-run. The other case will not be reset.');
      if(!initial.preflight.ok) throw Error('This demo is not ready. Check its preflight requirements.');
      await intro;
      if(!await step('start',ticket)) return;
      const started=await snapshot();
      if(!started.run) throw Error('The request did not start. Check the input or the error shown above.');
      currentId=started.run.id;
      if(!await waitFor(r=>r.state==='awaiting-approval',ticket,10000)) return;
      await speak(notes[1],ticket);
      if(!await gate(ticket)) return;
      // Approval starts with its explanation; results are narrated only after real completion.
      const computing=speak(notes[2],ticket);
      if(!await step('approve',ticket)) return;
      await computing;
      if(visitor) {
        if(!await gate(ticket)) return;
        const {run}=await snapshot();
        if(run?.state==='running') await speak(notes[3],ticket);
        if(!await waitFor(r=>r.state==='completed',ticket)) return;
        await speak(notes[4],ticket);await speak(notes[5],ticket);
      } else {
        if(!await waitFor(r=>r.state==='completed',ticket,45000)) return;
        await speak(notes[3],ticket);
        if(!await step('export',ticket)) return;
        if(!await waitFor(r=>r.export_refused,ticket,10000)) return;
        await speak(notes[4],ticket);await speak(notes[5],ticket);
      }
      if(await gate(ticket)) stop('Auto-run complete. Inspect the trust chain, explore all variants, or download the aggregate table. Reset this case to present it again.');
    } catch(error) {
      if(current(ticket)) stop(error.message);
    }
  };
})();
