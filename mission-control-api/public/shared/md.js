'use strict';
// The Markdown renderer for reports, shared by the Deck, the village and the
// Markets page. It was two identical copies inline; one fact, one place.
function mdToHtml(src){
  const esc = s => String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g,'$1<em>$2</em>')
    .replace(/(^|\s)_([^_\n]+)_(?=\s|$|[.,;:!?])/g,'$1<em>$2</em>');
  const out=[]; let list=null, table=null;
  const closeList=()=>{ if(list){out.push(`</${list}>`); list=null;} };
  const closeTable=()=>{ if(table){out.push('</tbody></table></div>'); table=null;} };
  for(const raw of String(src||'').split('\n')){
    const line=raw.replace(/\s+$/,'');
    const row=line.match(/^\|(.+)\|$/);
    if(row){
      const cells=row[1].split('|').map(c=>c.trim());
      if(cells.every(c=>/^:?-{2,}:?$/.test(c))) continue;      // separator row
      if(!table){ closeList();
        out.push('<div class="tw"><table><thead><tr>'+cells.map(c=>`<th>${inline(c)}</th>`).join('')+'</tr></thead><tbody>');
        table=1; continue; }
      out.push('<tr>'+cells.map(c=>`<td>${inline(c)}</td>`).join('')+'</tr>'); continue;
    }
    closeTable();
    if(!line.trim()){ closeList(); continue; }
    let m;
    if((m=line.match(/^(#{1,4})\s+(.*)$/))){ closeList(); const n=m[1].length; out.push(`<h${n}>${inline(m[2])}</h${n}>`); continue; }
    if(/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())){ closeList(); out.push('<hr>'); continue; }
    if((m=line.match(/^\s*[-*]\s+(.*)$/))){ if(list!=='ul'){closeList(); out.push('<ul>'); list='ul';} out.push(`<li>${inline(m[1])}</li>`); continue; }
    if((m=line.match(/^\s*\d+[.)]\s+(.*)$/))){ if(list!=='ol'){closeList(); out.push('<ol>'); list='ol';} out.push(`<li>${inline(m[1])}</li>`); continue; }
    if((m=line.match(/^>\s?(.*)$/))){ closeList(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    closeList(); out.push(`<p>${inline(line)}</p>`);
  }
  closeList(); closeTable();
  return out.join('\n');
}
