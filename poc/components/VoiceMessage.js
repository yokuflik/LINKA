// A clean, minimal voice-message player used inside message bubbles.
// Left: a play/pause toggle (triangle / two bars). Right: a thin progress
// track that fills as it plays, with an elapsed / total time readout.
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
    return { playing: false, currentTime: 0, audioDuration: 0, playedEmitted: false };
  },
  computed: {
    total() {
      return this.audioDuration || this.durationSeconds || 0;
    },
    progress() {
      return this.total > 0 ? Math.min(100, (this.currentTime / this.total) * 100) : 0;
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
      if (!a) return;
      this.currentTime = a.currentTime;
    },
    onEnded() {
      this.playing = false;
      this.currentTime = 0;
    },
    seek(event) {
      const a = this.$refs.audio;
      if (!a || !this.total) return;
      const rect = event.currentTarget.getBoundingClientRect();
      const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      a.currentTime = ratio * this.total;
    },
  },
  template: `
    <div class="flex items-center gap-2 py-1 min-w-[180px]">
      <button type="button" @click="toggle"
              class="shrink-0 w-8 h-8 rounded-full flex items-center justify-center"
              :class="mine ? 'bg-white/20 text-white' : 'bg-slate-200 text-slate-700'">
        <svg v-if="!playing" viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
        <svg v-else viewBox="0 0 24 24" class="w-4 h-4" fill="currentColor"><path d="M6 5h4v14H6zM14 5h4v14h-4z"/></svg>
      </button>
      <div class="flex-1 min-w-0 flex items-center gap-2">
        <div @click="seek"
             class="flex-1 h-1.5 rounded-full cursor-pointer"
             :class="mine ? 'bg-white/25' : 'bg-slate-300'">
          <div class="h-full rounded-full" :class="mine ? 'bg-white' : 'bg-teal-600'"
               :style="{ width: progress + '%' }"></div>
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
