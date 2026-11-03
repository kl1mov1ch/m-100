const {test}=require('node:test');
const assert=require('node:assert/strict');
const {rank}=require('../src/stroybot/static/search.js');
const items=[
  {id:'c6',code:'13',label:'Медная труба · 6,35 мм',name:'Фреонопровод медный Д6,35'},
  {id:'c9',code:'14',label:'Медная труба · 9,52 мм',name:'Фреонопровод медный Д9,52'},
  {id:'w25',code:'51',label:'Кабель · 3×2,5',name:'Кабель силовой, сечением 3х2,5-0,66'},
  {id:'w15',code:'52',label:'Кабель · 3×1,5',name:'Кабель силовой, сечением 3х1,5-0,66'},
  {id:'i6',code:'16',label:'Изоляция трубы · 6,35 мм',name:'Трубка теплоизоляционная Д6,35'},
];
test('Russian speech forms and diameter aliases',()=>{
  for(const query of ['медь 6-ка','медь6ка','медь 6-ку','медная 6,35','шестёрка'])
    assert.equal(rank(items,query)[0].id,'c6');
  assert.equal(rank(items,'медь 9-ка')[0].id,'c9');
});
test('cable dimensions are normalized without confusing numbers',()=>{
  for(const query of ['кабель 3 на 2,5','кабель 3x2.5','кабель 3х2,5'])
    assert.deepEqual(rank(items,query).map(i=>i.id),['w25']);
  assert.equal(rank(items,'кабель 3х4').length,0);
});
test('one-letter typos and transpositions',()=>{
  assert.equal(rank(items,'кабелб 3х2,5')[0].id,'w25');
  assert.equal(rank(items,'каблеь 3х2,5')[0].id,'w25');
});
test('exact position number and untrusted text',()=>{
  assert.equal(rank(items,'51')[0].id,'w25');
  assert.equal(rank(items,'<script>alert(1)</script>').length,0);
  assert.equal(rank(items,'неизвестная работа').length,0);
});
