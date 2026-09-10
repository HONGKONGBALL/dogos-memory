import test from 'node:test';
import assert from 'node:assert/strict';
import {extractReply,parseProcessJson} from '../runtime/dialogue_transcript.mjs';
const input=content=>({kind:{UserText:{content}}});
const speak=(text,extra={})=>({kind:{ToolCall:{name:'interact',arguments:JSON.stringify({segments:[{text,...extra}]})}}});
test('attributes only the requested native turn',()=>{
  assert.deepEqual(extractReply([input('earlier'),speak('old'),input('run-unique'),speak('你好')],'run-unique'),
    {spoken:'你好',tools:['interact']});
});
test('refuses an unrelated completed reply',()=>assert.throws(()=>extractReply([input('elsewhere'),speak('wrong')],'run-unique')));
test('refuses concurrent user input before forwarding',()=>assert.throws(()=>extractReply([input('run-unique'),speak('hello'),input('another person')],'run-unique'),/Concurrent/));
test('refuses tool output containing an action',()=>assert.throws(()=>extractReply([input('run-unique'),speak('hello',{action:'jump'})],'run-unique'),/Non-speech/));
test('refuses further relay after a noncommunication tool',()=>assert.throws(()=>extractReply([input('run-unique'),{kind:{ToolCall:{name:'pose',arguments:'{}'}}}],'run-unique'),/Unexpected tool/));
test('accepts indented connection reports after a runtime warning',()=>assert.deepEqual(
  parseProcessJson('runtime warning\n{\n  "state": "ready",\n  "dogs": []\n}\n'),{state:'ready',dogs:[]}));
test('rejects an incomplete connection report',()=>assert.throws(()=>parseProcessJson('{\n  "state":')));
