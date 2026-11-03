class M100PCM extends AudioWorkletProcessor {
  constructor(){super();this.ratio=sampleRate/16000;this.total=0;this.next=this.ratio;this.sum=0;this.count=0;this.buffer=[];this.port.onmessage=e=>{if(e.data==='flush'){this.flush();this.port.postMessage({flushed:true});}};}
  flush(){if(!this.buffer.length)return;const pcm=new Int16Array(this.buffer);this.buffer=[];this.port.postMessage(pcm.buffer,[pcm.buffer]);}
  process(inputs,outputs){for(const channel of outputs[0]||[])channel.fill(0);const input=inputs[0]?.[0];if(!input)return true;for(const v of input){this.sum+=v;this.count++;this.total++;if(this.total>=this.next){const value=Math.max(-1,Math.min(1,this.sum/this.count));this.buffer.push(Math.round(value*(value<0?32768:32767)));this.sum=0;this.count=0;this.next+=this.ratio;if(this.buffer.length>=1600)this.flush();}}return true;}
}
registerProcessor('m100-pcm',M100PCM);
