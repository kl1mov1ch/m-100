/* Local search: no AI requests per keystroke; numbers are matched exactly. */
(function (root) {
  "use strict";
  function normalize(value) {
    return String(value || "").toLowerCase().replace(/ё/g, "е")
      .replace(/(\d)\s*(?:на|[xх×*])\s*(\d)/g, "$1х$2")
      .replace(/,/g, ".").replace(/\b([69])[- ]?ка\b/g, "$1ка")
      .replace(/шестерка|шестёрка|6[- ]?(?:ка|ку)/g, "6.35")
      .replace(/девятка|9[- ]?(?:ка|ку)/g, "9.52")
      .replace(/12[- ]?(?:ка|ку)/g, "12.7")
      .replace(/медь|медны[йехм]|медная|медной/g, "мед")
      .replace(/теплоизоляци\S*|изоляци\S*/g, "изоляц")
      .replace(/([а-я])(\d)/g, "$1 $2")
      .replace(/[^a-zа-я0-9.х]+/g, " ").trim().replace(/\s+/g, " ");
  }
  function near(a, b) {
    if (Math.abs(a.length - b.length) > 1) return false;
    let i=0, j=0, changes=0;
    while (i<a.length && j<b.length) {
      if (a[i]===b[j]) { i++; j++; continue; }
      if (++changes>1) return false;
      if (a.length===b.length && a[i]===b[j+1] && a[i+1]===b[j]) { i+=2; j+=2; continue; }
      if (a.length>=b.length) i++;
      if (b.length>=a.length) j++;
    }
    return changes+(i<a.length || j<b.length ? 1:0)<=1;
  }
  function rank(items, query) {
    const tokens=normalize(query).split(" ").filter(Boolean);
    if (!tokens.length) return items.slice();
    return items.map((item,index)=> {
      const text=normalize(`${item.label} ${item.name} ${item.code}`);
      const words=text.split(" "); let score=String(item.code)===String(query).trim()?100:0;
      for (const token of tokens) {
        if (/^\d+(?:[.х]\d+)*$/.test(token)) {
          if (!words.some(w=>w===token || w===`д${token}` || w===`ф${token}` || w.startsWith(token+"-"))) return null;
          score+=10;
        } else if (words.includes(token)) score+=12;
        else if (words.some(w=>w.startsWith(token))) score+=8;
        else if (text.includes(token)) score+=5;
        else if (token.length>=5 && words.some(w=>w.length>=5 && near(token,w))) score+=2;
        else return null;
      }
      return {item,index,score};
    }).filter(Boolean).sort((a,b)=>b.score-a.score || a.index-b.index).map(v=>v.item);
  }
  const api={normalize,rank};
  if (typeof module!=="undefined" && module.exports) module.exports=api;
  else root.M100Search=api;
})(typeof window!=="undefined"?window:globalThis);
