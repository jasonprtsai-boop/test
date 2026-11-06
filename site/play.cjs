const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const {spawn} = require('node:child_process');
const engineDir = path.join(__dirname, '..', 'engine_bin');
function engine(moves, search) {
  return new Promise((resolve, reject) => {
    const child = spawn(path.join(engineDir, 'pikafish.exe'), [], {cwd: engineDir, windowsHide:true});
    let output = '';
    const timer = setTimeout(() => child.kill(), 12000);
    child.once('error', reject);
    child.stdin.on('error', reject);
    child.stdout.on('data', chunk => {
      output += chunk;
      if (search ? /^bestmove (\S+)/m.test(output) : /Nodes searched: \d+/.test(output)) {
        const result = search ? output.match(/^bestmove (\S+)/m)[1] : [...output.matchAll(/^([a-i]\d[a-i]\d): 1/gm)].map(m=>m[1]);
        resolve(result);
        child.kill();
      }
    });
    child.once('close', () => {clearTimeout(timer); reject(Error('引擎已停止，請重試'));});
    child.stdin.write('position startpos moves '+moves.join(' ')+'\ngo '+(search?'movetime 300':'perft 1')+'\n');
  });
}
const server = http.createServer(async (req,res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if(url.pathname === '/') {res.setHeader('Content-Type','text/html; charset=utf-8'); return res.end(fs.readFileSync(path.join(__dirname,'play.html')));}
    if(url.pathname !== '/turn') {res.writeHead(404); return res.end();}
    const moves = (url.searchParams.get('moves') || '').split(',').filter(Boolean);
    if(!moves.every(m=>/^[a-i]\d[a-i]\d$/.test(m))) {res.writeHead(400); return res.end();}
    let legal = await engine(moves,false), reply = null;
    if(moves.length%2 && legal.length) {
      reply = await engine(moves,true);
      if(!legal.includes(reply)) throw Error('引擎回應無效');
      legal = await engine([...moves,reply],false);
    }
    res.setHeader('Cache-Control','no-store');
    res.end(JSON.stringify({reply,legal}));
  } catch(e) {res.writeHead(500); res.end(e.message);}
});
server.listen(0,'127.0.0.1',()=>{
 const url='http://127.0.0.1:'+server.address().port;
 console.log(url);
 if(process.argv.includes('--open')) spawn('rundll32.exe',['url.dll,FileProtocolHandler',url],{windowsHide:true});
});
