const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
test('PCM resamples 48 kHz to 16 kHz without playback or lost block boundaries',()=>{
  let Processor;const messages=[];
  const scope={sampleRate:48000,AudioWorkletProcessor:class{constructor(){this.port={postMessage:data=>messages.push(data)};}},registerProcessor:(name,ctor)=>{Processor=ctor;}};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../src/stroybot/static/pcm-worklet.js'),'utf8'),scope);
  const rec=new Processor(),output=new Float32Array(128).fill(1),input=new Float32Array(128).fill(.5);
  for(let n=0;n<375;n++)rec.process([[input]],[[output]]);
  rec.port.onmessage({data:'flush'});
  const arrays=messages.filter(v=>Object.prototype.toString.call(v)==='[object ArrayBuffer]').map(v=>new Int16Array(v));
  assert.equal(arrays.reduce((n,v)=>n+v.length,0),16000);
  assert.ok(arrays.every(v=>Array.from(v).every(x=>x===16384)));
  assert.ok(output.every(v=>v===0));
  assert.ok(messages.some(v=>v.flushed));
});
