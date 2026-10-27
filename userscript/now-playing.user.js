// ==UserScript==
// @name         Now Playing -> local bridge
// @description  Sends the real title / artist / artwork from YouTube, YouTube Music and SoundCloud to the local MPRIS bridge
// @match        https://music.youtube.com/*
// @match        https://www.youtube.com/*
// @match        https://soundcloud.com/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

(function () {
    'use strict';
    const BRIDGE = 'http://127.0.0.1:5000/meta';
    const win = (typeof unsafeWindow !== 'undefined') ? unsafeWindow : window;

    // Pick the largest artwork entry from a MediaSession artwork list
    function biggest(artwork) {
        if (!artwork || !artwork.length) return '';
        let best = artwork[0], bestArea = 0;
        for (const a of artwork) {
            const m = /(\d+)x(\d+)/.exec(a.sizes || '');
            const area = m ? (+m[1]) * (+m[2]) : 0;
            if (area >= bestArea) { best = a; bestArea = area; }
        }
        return best.src || '';
    }

    function fromMediaSession() {
        const ms = win.navigator.mediaSession;
        const m = ms && ms.metadata;
        if (!m || !m.title) return null;
        return { title: m.title, artist: m.artist || '', album: m.album || '', art: biggest(m.artwork) };
    }

    // Best-effort fallback if the page has not filled in MediaSession
    function fromDom() {
        const text = sel => (document.querySelector(sel) || {}).textContent;
        if (location.hostname.endsWith('soundcloud.com')) {
            // SoundCloud's playback bar. These class names are best-effort and may change.
            const badge = document.querySelector('.playbackSoundBadge');
            if (!badge) return null;
            const link = badge.querySelector('.playbackSoundBadge__titleLink');
            const hidden = link && link.querySelector('[aria-hidden="true"]');
            const title = ((link && link.getAttribute('title')) || (hidden && hidden.textContent) || '').trim();
            const artist = ((badge.querySelector('.playbackSoundBadge__lightLink') || {}).textContent || '').trim();
            const cover = badge.querySelector('.playbackSoundBadge__avatar span');
            const m = cover && /url\(["']?([^"')]+)/.exec(cover.style.backgroundImage || '');
            const art = m ? m[1].replace(/-t\d+x\d+\./, '-t500x500.') : '';   // ask for the larger cover
            return title ? { title, artist, album: '', art } : null;
        }
        if (location.hostname === 'music.youtube.com') {
            const title = (text('ytmusic-player-bar .title') || '').trim();
            const byline = (text('ytmusic-player-bar .byline') || '').trim();
            const img = document.querySelector('ytmusic-player-bar img.image, #song-image img');
            const parts = byline.split('•').map(x => x.trim());
            return title ? { title, artist: parts[0] || '', album: parts[1] || '', art: img ? img.src : '' } : null;
        }
        const title = (text('h1.ytd-watch-metadata yt-formatted-string') || '').trim();
        const artist = (text('ytd-watch-metadata #owner ytd-channel-name a') || '').trim();
        return title ? { title, artist, album: '', art: '' } : null;
    }

    // Is something loaded, and is it playing? Uses the page's media element when there is one.
    function mediaState() {
        const el = document.querySelector('video') || document.querySelector('audio');
        if (el && el.readyState > 0) {
            return { playing: !el.paused && !el.ended, duration: isFinite(el.duration) ? el.duration : 0 };
        }
        // SoundCloud may keep its audio element out of the page, so read the play button instead
        if (location.hostname.endsWith('soundcloud.com') && document.querySelector('.playbackSoundBadge')) {
            return { playing: !!document.querySelector('.playControls__play.playing'), duration: 0 };
        }
        return null;
    }

    function collect() {
        const d = fromMediaSession() || fromDom();
        if (!d || !d.title) return null;
        d.url = location.href;
        if (!d.art) {
            const id = new URLSearchParams(location.search).get('v');
            if (id) d.art = 'https://i.ytimg.com/vi/' + id + '/mqdefault.jpg';
        }
        return d;
    }

    function send(d) {
        const body = JSON.stringify(d);
        if (typeof GM_xmlhttpRequest === 'function') {
            GM_xmlhttpRequest({
                method: 'POST', url: BRIDGE, data: body,
                headers: { 'Content-Type': 'application/json' },
                onload: r => { if (r.status !== 204) console.warn('[now-playing] bridge replied', r.status); },
                              onerror: e => console.warn('[now-playing] could not reach bridge', e),
                              ontimeout: () => console.warn('[now-playing] bridge timed out')
            });
        } else {
            fetch(BRIDGE, { method: 'POST', mode: 'no-cors', headers: { 'Content-Type': 'text/plain' }, body })
            .catch(() => {});
        }
    }

    console.log('[now-playing] userscript loaded on', location.hostname);

    let lastSig = '';
    let lastSent = 0;
    let ticks = 0;

    function tick() {
        ticks++;
        const state = mediaState();
        if (!state) return;                                    // nothing loaded in this tab yet
        const d = collect();
        if (!d) {
            if (ticks % 25 === 0) console.warn('[now-playing] player found but no title/artist metadata could be read');
            return;
        }
        d.playing = state.playing;
        d.duration = state.duration;

        // Send the moment anything changes (new track, art, length, play/pause), otherwise every 1.5 s as a heartbeat
        const sig = [d.title, d.artist, d.art, Math.round(d.duration), d.playing].join('|');
        const now = Date.now();
        if (sig !== lastSig || now - lastSent >= 1500) {
            if (sig.split('|')[0] !== lastSig.split('|')[0]) console.log('[now-playing] track:', d.title, '-', d.artist);
            lastSig = sig;
            lastSent = now;
            send(d);
        }
    }

    setInterval(tick, 400);
    // react straight away when the player loads a new track (media events don't bubble, so listen in the capture phase)
    ['loadedmetadata', 'durationchange', 'playing', 'pause', 'emptied'].forEach(ev =>
    document.addEventListener(ev, () => setTimeout(tick, 50), true));
})();
