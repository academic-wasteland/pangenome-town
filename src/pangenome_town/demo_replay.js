/* Static replay adapter: API calls are answered inside this browser, never forwarded to a town. */
(() => {
  const originalFetch=window.fetch.bind(window), prefix=window.DEMO_REPLAY_BASE;
  const records=originalFetch(prefix+'assets/recordings.json').then(response=>{
    if(!response.ok) throw Error('Recorded demonstration data could not be loaded.');
    return response.json();
  });
  const sessions={};
  const copy=value=>JSON.parse(JSON.stringify(value));
  const key=caseName=>'wasteland-public-replay-v1:'+caseName;
  function session(caseName) {
    if(!sessions[caseName]) {
      try {sessions[caseName]=JSON.parse(sessionStorage.getItem(key(caseName)))||{phase:'idle'};}
      catch {sessions[caseName]={phase:'idle'};}
    }
    return sessions[caseName];
  }
  function save(caseName) {sessionStorage.setItem(key(caseName),JSON.stringify(sessions[caseName]));}
  function snapshot(caseName,recording) {
    const s=session(caseName);
    if(s.phase==='idle') return {preflight:recording.start.preflight,run:null};
    if(s.phase==='start') return copy(recording.start);
    if(s.phase==='export') return copy(recording.export);
    if(s.phase==='completed') return copy(recording.completed);
    // Show actual recorded events progressively; shortened replay time is explicitly labelled.
    const after=recording.completed.run.events.filter(e=>e.seq>recording.start.run.events.length);
    const n=Math.min(after.length,Math.floor((Date.now()-s.started)/700)+1);
    if(n>=after.length) {s.phase='completed';save(caseName);return copy(recording.completed);}
    const result=copy(recording.start);
    result.run.state='running';
    result.run.decisions=copy(recording.completed.run.decisions);
    result.run.events.push(...copy(after.slice(0,n)));
    if(caseName==='demo-cluster') {
      const progress=result.run.events.filter(e=>e.detail?.scheduler_state).at(-1);
      result.run.job_id=progress?.detail.job_id||null;
      result.run.scheduler_state=progress?.detail.scheduler_state||null;
    }
    return result;
  }
  window.fetch=async(input,options={})=>{
    const path=new URL(typeof input==='string'?input:input.url,location.href).pathname;
    const match=path.match(/^\/api\/(demo(?:-cluster)?)(?:\/(start|approve|export|reset|trust))?$/);
    if(!match) return originalFetch(input,options);
    try {
      const [,caseName,action]=match, recording=(await records)[caseName], s=session(caseName);
      let result;
      if(action==='trust') {
        result=s.phase==='idle'?{run_id:null,receivers:[]}:copy(s.phase==='start'?recording.trust_start:recording.trust_completed);
      } else if(options.method==='POST') {
        const body=JSON.parse(options.body||'{}');
        if(action!=='start' && s.phase!=='idle' && body.run_id!==recording.start.run.id) throw Error('This page refers to a different recorded run.');
        if(action==='reset') {
          if(snapshot(caseName,recording).run?.state==='running') throw Error('Wait for this replay to finish before resetting.');
          s.phase='idle';
        } else if(action==='start') {
          if(s.phase!=='idle') throw Error('Reset this case before replaying it.');
          if(body.vcf) throw Error('Public playback uses the recorded synthetic file; no uploads are accepted.');
          s.phase='start';s.presented=new Date().toISOString();
        } else if(action==='approve') {
          if(s.phase!=='start') throw Error('This replay is not awaiting its approval step.');
          s.phase='running';s.started=Date.now();
        } else if(action==='export') {
          if(caseName!=='demo'||snapshot(caseName,recording).run?.state!=='completed') throw Error('Complete the cohort replay first.');
          s.phase='export';
        } else throw Error('Unknown playback action.');
        save(caseName);result=snapshot(caseName,recording);
      } else result=snapshot(caseName,recording);
      if(result?.run && s.presented) result.run.created=s.presented;
      return new Response(JSON.stringify(result),{headers:{'Content-Type':'application/json'}});
    } catch(error) {
      return new Response(JSON.stringify({error:error.message}),{status:400,headers:{'Content-Type':'application/json'}});
    }
  };
})();
