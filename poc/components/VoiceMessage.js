// A WhatsApp-style voice-message player used inside message bubbles.
// Left: a play/pause toggle. Right: a static waveform (decoded once from the
// audio via the Web Audio API) with a draggable scrubber thumb, plus an
// elapsed/total time readout. The played portion of the waveform is tinted.
// No mic icon - the bubble context already makes it a voice message.
// Registered globally in index.html (app.component('VoiceMessage', ...)).
const VoiceMessage = {
  props: {
    src: { type: String, required: true },
    // recorded length in seconds (from media_duration_seconds); used as the
    // total until the browser reports its own duration on metadata load
    durationSeconds: { type: Number, default: 0 },
    // true when this bubble is the current user's own message (tints controls)
    mine: { type: Boolean, default: false },
  },
  emits: ['played'], // fired the moment the listener starts playing it (drives the "played" receipt)
  data() {
    return {
      playing: false,
      currentTime: 0,
      audioDuration: 0,
      playedEmitted: false,
      // peak amplitudes (0..1); a flat placeholder until decodeWaveform resolves
      peaks: new Array(48).fill(0.3),
      // true while the user drags the scrubber - suppresses timeupdate writes
      scrubbing: false,
    };
  },
  computed: {
    total() {
      return this.audioDuration || this.durationSeconds || 0;
    },
    // 0..1 playback position, used for the thumb and the waveform tint
    ratio() {
      return this.total > 0 ? Math.min(1, this.currentTime / this.total) : 0;
    },
    playedBars() {
      return Math.round(this.ratio * this.peaks.length);
    },
    timeLabel() {
      const shown = this.playing || this.currentTime > 0 ? this.currentTime : this.total;
      return this.fmt(shown);
    },
  },
  methods: {
    fmt(s) {
      s = Math.max(0, Math.round(s || 0));
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    },
    async decodeWaveform() {
      const helper = window.__linkaWaveform;
      if (!helper || !helper.peaksForUrl) return;
      try {
        this.peaks = await helper.peaksForUrl(this.src);
      } catch (e) {
        /* keep the placeholder */
      }
    },
    toggle() {
      const a = this.$refs.audio;
      if (!a) return;
      if (this.playing) a.pause();
      else a.play();
    },
    onPlay() {
      this.playing = true;
      // "Played" counts from the moment the listener starts hearing it,
      // not from finishing it - matches WhatsApp's "listened" behaviour.
      if (!this.playedEmitted) {
        this.playedEmitted = true;
        this.$emit('played');
      }
    },
    onTimeUpdate() {
      const a = this.$refs.audio;
      if (!a || this.scrubbing) return;
      this.currentTime = a.currentTime;
    },
    onEnded() {
      this.playing = false;
      this.currentTime = 0;
    },
    // <input type="range"> drives both the drag preview and the final seek.
    onScrubInput(event) {
      this.scrubbing = true;
      this.currentTime = (Number(event.target.value) / 1000) * this.total;
    },
    onScrubChange(event) {
      const a = this.$refs.audio;
      const t = (Number(event.target.value) / 1000) * this.total;
      if (a && isFinite(t)) a.currentTime = t;
      this.currentTime = t;
      this.scrubbing = false;
    },
  },
  watch: {
    src() {
      this.peaks = new Array(48).fill(0.3);
      this.decodeWaveform();
    },
  },
  mounted() {
    this.decodeWaveform();
  },
  template: `
    <div class="flex items-center gap-2 py-1 min-w-[200px]">
      <button type="button" @click="toggle"
              class="shrink-0 w-8 h-8 rounded-full flex items-center justify-center"
              :class="mine ? 'bg-white/20 text-white' : 'bg-slate-200 text-slate-700'">
        <svg v-if="!playing" viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        <svg v-else viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
      </button>
      <div class="flex-1 min-w-0 flex items-center gap-2">
        <div class="relative flex-1 min-w-0 h-8 flex items-center">
          <!-- Static waveform; bars before the playhead are tinted "played" -->
          <div class="absolute inset-0 flex items-center gap-[2px] pointer-events-none">
            <span v-for="(p, i) in peaks" :key="i"
                  class="flex-1 rounded-full transition-colors"
                  :style="{ height: Math.max(10, p * 100) + '%' }"
                  :class="i < playedBars
                    ? (mine ? 'bg-white' : 'bg-teal-600')
                    : (mine ? 'bg-white/30' : 'bg-slate-300')"></span>
          </div>
          <!-- Draggable scrubber thumb, positioned over the waveform -->
          <span class="absolute top-1/2 -translate-y-1/2 -translate-x-1/2 w-3 h-3 rounded-full shadow pointer-events-none"
                :class="mine ? 'bg-white' : 'bg-teal-600'"
                :style="{ left: (ratio * 100) + '%' }"></span>
          <!-- Transparent native range on top captures drag/click -->
          <input type="range" min="0" max="1000" step="1"
                 :value="Math.round(ratio * 1000)"
                 :disabled="!total"
                 @input="onScrubInput" @change="onScrubChange"
                 class="vm-scrub absolute inset-0 w-full h-full m-0 cursor-pointer opacity-0" />
        </div>
        <div class="shrink-0 text-[11px] tabular-nums" :class="mine ? 'text-white/70' : 'text-slate-500'">
          {{ timeLabel }}
        </div>
      </div>
      <audio ref="audio" :src="src" preload="metadata"
             @loadedmetadata="audioDuration = isFinite($refs.audio.duration) ? $refs.audio.duration : 0"
             @timeupdate="onTimeUpdate"
             @play="onPlay" @pause="playing = false"
             @ended="onEnded"></audio>
    </div>
  `,
};
