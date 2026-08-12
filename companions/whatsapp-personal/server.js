'use strict';

const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const path = require('path');
const qrcode = require('qrcode-terminal');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');

const port = Number(process.env.WHATSAPP_PERSONAL_PORT || 46668);
const relayUrl = (process.env.WHATSAPP_PERSONAL_RELAY_URL || 'http://127.0.0.1:46667').replace(/\/$/, '');
const token = process.env.WHATSAPP_PERSONAL_COMPANION_TOKEN || '';
const secret = process.env.WHATSAPP_PERSONAL_WEBHOOK_SECRET || '';
const sessionDir = path.resolve(process.env.WHATSAPP_PERSONAL_SESSION_DIR || path.join(__dirname, '.session'));
const maxMedia = Number(process.env.WHATSAPP_PERSONAL_MAX_MEDIA_BYTES || 25 * 1024 * 1024);
let ready = false;
let lastError = null;

if (!token || !secret) throw new Error('Companion token and webhook secret are required');
const client = new Client({authStrategy: new LocalAuth({dataPath: sessionDir}),
  puppeteer: {headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox']}});
client.on('qr', qr => qrcode.generate(qr, {small: true}));
client.on('ready', () => { ready = true; lastError = null; console.log('WhatsApp Personal ready'); });
client.on('disconnected', reason => { ready = false; lastError = String(reason); });
client.on('auth_failure', error => { ready = false; lastError = String(error); });

async function relayMessage(message) {
  const chat = await message.getChat();
  const contact = await message.getContact();
  const payload = {message_id: message.id._serialized, chat_id: message.from,
    chat_name: chat.name || contact.pushname || message.from, is_group: Boolean(chat.isGroup),
    author_id: message.author || message.from, author_name: contact.pushname || contact.name || message.author,
    text: message.body || '', timestamp: message.timestamp};
  if (message.hasMedia) {
    const media = await message.downloadMedia();
    if (media && Buffer.byteLength(media.data, 'base64') <= maxMedia)
      payload.media = {data: media.data, mime_type: media.mimetype,
        name: media.filename || `${message.id.id}.bin`};
  }
  const body = JSON.stringify(payload);
  const signature = 'sha256=' + crypto.createHmac('sha256', secret).update(body).digest('hex');
  const response = await fetch(`${relayUrl}/v1/webhooks/whatsapp-personal`, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-WhatsApp-Personal-Signature': signature}, body});
  if (!response.ok) throw new Error(`Relay webhook failed: ${response.status} ${await response.text()}`);
}
client.on('message', message => relayMessage(message).catch(error => { lastError = String(error); console.error(error); }));
client.initialize().catch(error => { lastError = String(error); console.error(error); });

function json(response, status, value) { const body = JSON.stringify(value); response.writeHead(status,
  {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body)}); response.end(body); }
function authorized(request) { return request.headers.authorization === `Bearer ${token}`; }
async function body(request) { const chunks = []; for await (const chunk of request) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString('utf8')); }

http.createServer(async (request, response) => {
  try {
    if (!authorized(request)) return json(response, 401, {error: 'Bearer token required'});
    if (request.method === 'GET' && request.url === '/status')
      return json(response, 200, {ready, lastError, sessionDir});
    if (request.method === 'GET' && request.url === '/chats') {
      const chats = await client.getChats(); return json(response, 200, {chats: chats.map(chat =>
        ({id: chat.id._serialized, name: chat.name, is_group: Boolean(chat.isGroup), unread: chat.unreadCount}))});
    }
    if (request.method === 'SEND' || (request.method === 'POST' && request.url === '/send')) {
      const input = await body(request); const sent = [];
      if (input.text) sent.push((await client.sendMessage(input.chat_id, input.text)).id._serialized);
      for (const filename of input.attachments || []) {
        if (!fs.statSync(filename).isFile()) throw new Error(`Attachment is not a file: ${filename}`);
        sent.push((await client.sendMessage(input.chat_id, MessageMedia.fromFilePath(filename))).id._serialized);
      }
      return json(response, 200, {sent});
    }
    return json(response, 404, {error: 'not found'});
  } catch (error) { lastError = String(error); return json(response, 400, {error: String(error)}); }
}).listen(port, '127.0.0.1', () => console.log(`WhatsApp Personal companion on 127.0.0.1:${port}`));
