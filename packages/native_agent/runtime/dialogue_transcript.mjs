import assert from 'node:assert/strict';

export function parseProcessJson(output) {
  for(const match of output.matchAll(/^\{/gm)){
    try{return JSON.parse(output.slice(match.index).trim())}catch{}
  }
  throw new Error('No complete JSON result from device check');
}

export function extractReply(entries, marker) {
  let started=false;
  const speech=[];
  const tools=[];
  for(const entry of entries){
    const kind=entry.kind||{};
    if(kind.UserText){
      if(kind.UserText.content.includes(marker)){started=true;continue}
      if(started)throw new Error('Concurrent input observed; stopping relay');
    }
    if(!started||!kind.ToolCall)continue;
    const call=kind.ToolCall;tools.push(call.name);
    assert(['interact','end_turn'].includes(call.name),`Unexpected tool ${call.name}; no next turn`);
    if(call.name==='interact'){
      const args=JSON.parse(call.arguments);
      assert.deepEqual(Object.keys(args),['segments']);
      assert(Array.isArray(args.segments));
      for(const segment of args.segments){
        assert.deepEqual(Object.keys(segment),['text'],'Non-speech segment; stopping relay');
        assert.equal(typeof segment.text,'string');speech.push(segment.text);
      }
    }
  }
  const spoken=speech.join(' ').trim();
  assert(started&&spoken&&spoken.length<=1000,'No attributable bounded reply');
  return {spoken,tools};
}
