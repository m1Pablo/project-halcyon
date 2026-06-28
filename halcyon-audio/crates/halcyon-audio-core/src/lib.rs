use serde::{Deserialize, Serialize};
use std::f32::consts::PI;

pub const DEFAULT_POLYPHONY: usize = 8;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum SynthCommand {
    NoteOn {
        note: u8,
        velocity: u8,
        #[serde(default = "default_channel")]
        channel: u8,
    },
    NoteOff {
        note: u8,
        #[serde(default = "default_channel")]
        channel: u8,
    },
    AllNotesOff,
    SetParam {
        name: String,
        value: f32,
    },
    Shutdown,
}

fn default_channel() -> u8 {
    1
}

#[derive(Clone, Copy, Debug)]
pub struct SynthParams {
    pub master_gain: f32,
    pub saw_square_blend: f32,
    pub cutoff_hz: f32,
    pub attack_seconds: f32,
    pub decay_seconds: f32,
    pub sustain_level: f32,
    pub release_seconds: f32,
}

impl Default for SynthParams {
    fn default() -> Self {
        Self {
            master_gain: 0.24,
            saw_square_blend: 0.45,
            cutoff_hz: 3_600.0,
            attack_seconds: 0.006,
            decay_seconds: 0.12,
            sustain_level: 0.72,
            release_seconds: 0.25,
        }
    }
}

impl SynthParams {
    pub fn set(&mut self, name: &str, value: f32) -> bool {
        match name {
            "master_gain" => self.master_gain = value.clamp(0.0, 1.0),
            "saw_square_blend" => self.saw_square_blend = value.clamp(0.0, 1.0),
            "cutoff_hz" => self.cutoff_hz = value.clamp(40.0, 20_000.0),
            "attack_seconds" => self.attack_seconds = value.clamp(0.0, 5.0),
            "decay_seconds" => self.decay_seconds = value.clamp(0.0, 5.0),
            "sustain_level" => self.sustain_level = value.clamp(0.0, 1.0),
            "release_seconds" => self.release_seconds = value.clamp(0.0, 8.0),
            _ => return false,
        }
        true
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EnvelopeStage {
    Attack,
    Decay,
    Sustain,
    Release,
    Done,
}

#[derive(Clone, Copy, Debug)]
struct Envelope {
    stage: EnvelopeStage,
    level: f32,
    release_step: f32,
}

impl Envelope {
    fn new() -> Self {
        Self {
            stage: EnvelopeStage::Attack,
            level: 0.0,
            release_step: 0.0,
        }
    }

    fn release(&mut self, release_seconds: f32, sample_rate: f32) {
        if self.stage == EnvelopeStage::Done {
            return;
        }
        if release_seconds <= f32::EPSILON {
            self.level = 0.0;
            self.stage = EnvelopeStage::Done;
            self.release_step = 0.0;
            return;
        }
        self.stage = EnvelopeStage::Release;
        self.release_step = self.level / (release_seconds * sample_rate).max(1.0);
    }

    fn next(&mut self, params: SynthParams, sample_rate: f32) -> f32 {
        match self.stage {
            EnvelopeStage::Attack => {
                if params.attack_seconds <= f32::EPSILON {
                    self.level = 1.0;
                    self.stage = EnvelopeStage::Decay;
                } else {
                    self.level += 1.0 / (params.attack_seconds * sample_rate).max(1.0);
                    if self.level >= 1.0 {
                        self.level = 1.0;
                        self.stage = EnvelopeStage::Decay;
                    }
                }
            }
            EnvelopeStage::Decay => {
                let sustain = params.sustain_level;
                if params.decay_seconds <= f32::EPSILON {
                    self.level = sustain;
                    self.stage = EnvelopeStage::Sustain;
                } else {
                    let step = (1.0 - sustain) / (params.decay_seconds * sample_rate).max(1.0);
                    self.level -= step;
                    if self.level <= sustain {
                        self.level = sustain;
                        self.stage = EnvelopeStage::Sustain;
                    }
                }
            }
            EnvelopeStage::Sustain => {
                self.level = params.sustain_level;
            }
            EnvelopeStage::Release => {
                self.level -= self.release_step;
                if self.level <= 0.0 {
                    self.level = 0.0;
                    self.stage = EnvelopeStage::Done;
                }
            }
            EnvelopeStage::Done => {
                self.level = 0.0;
            }
        }
        self.level
    }

    fn is_done(&self) -> bool {
        self.stage == EnvelopeStage::Done
    }
}

#[derive(Clone, Debug)]
struct Voice {
    note: u8,
    channel: u8,
    frequency_hz: f32,
    velocity_gain: f32,
    phase: f32,
    filter_state: f32,
    envelope: Envelope,
}

impl Voice {
    fn new(note: u8, velocity: u8, channel: u8) -> Self {
        Self {
            note,
            channel,
            frequency_hz: midi_note_to_hz(note),
            velocity_gain: f32::from(velocity.clamp(1, 127)) / 127.0,
            phase: 0.0,
            filter_state: 0.0,
            envelope: Envelope::new(),
        }
    }

    fn release(&mut self, release_seconds: f32, sample_rate: f32) {
        self.envelope.release(release_seconds, sample_rate);
    }

    fn render(&mut self, params: SynthParams, sample_rate: f32) -> f32 {
        let saw = (self.phase * 2.0) - 1.0;
        let square = if self.phase < 0.5 { 1.0 } else { -1.0 };
        let blend = params.saw_square_blend;
        let oscillator = saw.mul_add(1.0 - blend, square * blend);

        self.phase = (self.phase + self.frequency_hz / sample_rate).fract();

        let envelope = self.envelope.next(params, sample_rate);
        let cutoff = params.cutoff_hz.min(sample_rate * 0.45).max(40.0);
        let alpha = 1.0 - (-2.0 * PI * cutoff / sample_rate).exp();
        self.filter_state += alpha * (oscillator - self.filter_state);

        self.filter_state * envelope * self.velocity_gain
    }

    fn is_done(&self) -> bool {
        self.envelope.is_done()
    }
}

#[derive(Clone, Debug)]
pub struct SynthEngine {
    sample_rate: f32,
    polyphony: usize,
    params: SynthParams,
    voices: Vec<Voice>,
}

impl SynthEngine {
    pub fn new(sample_rate: f32) -> Self {
        Self::with_polyphony(sample_rate, DEFAULT_POLYPHONY)
    }

    pub fn with_polyphony(sample_rate: f32, polyphony: usize) -> Self {
        let polyphony = polyphony.max(1);
        Self {
            sample_rate: sample_rate.max(1.0),
            polyphony,
            params: SynthParams::default(),
            voices: Vec::with_capacity(polyphony),
        }
    }

    pub fn params(&self) -> SynthParams {
        self.params
    }

    pub fn active_voice_count(&self) -> usize {
        self.voices.len()
    }

    pub fn handle_command(&mut self, command: SynthCommand) -> bool {
        match command {
            SynthCommand::NoteOn {
                note,
                velocity,
                channel,
            } => {
                if velocity == 0 {
                    self.release_note(note, channel);
                } else {
                    self.start_note(note, velocity, channel);
                }
                false
            }
            SynthCommand::NoteOff { note, channel } => {
                self.release_note(note, channel);
                false
            }
            SynthCommand::AllNotesOff => {
                self.release_all();
                false
            }
            SynthCommand::SetParam { name, value } => {
                self.params.set(&name, value);
                false
            }
            SynthCommand::Shutdown => {
                self.voices.clear();
                true
            }
        }
    }

    pub fn render_sample(&mut self) -> f32 {
        let mut sample = 0.0;
        let params = self.params;
        let sample_rate = self.sample_rate;

        for voice in &mut self.voices {
            sample += voice.render(params, sample_rate);
        }
        self.voices.retain(|voice| !voice.is_done());

        soft_clip(sample * self.params.master_gain)
    }

    pub fn render_block(&mut self, output: &mut [f32]) {
        for sample in output {
            *sample = self.render_sample();
        }
    }

    pub fn render_offline(&mut self, seconds: f32) -> Vec<f32> {
        let frame_count = (seconds.max(0.0) * self.sample_rate).round() as usize;
        let mut output = vec![0.0; frame_count];
        self.render_block(&mut output);
        output
    }

    fn start_note(&mut self, note: u8, velocity: u8, channel: u8) {
        self.voices
            .retain(|voice| !(voice.note == note && voice.channel == channel && voice.is_done()));
        if self.voices.len() >= self.polyphony {
            self.voices.remove(0);
        }
        self.voices.push(Voice::new(note, velocity, channel));
    }

    fn release_note(&mut self, note: u8, channel: u8) {
        for voice in &mut self.voices {
            if voice.note == note && voice.channel == channel {
                voice.release(self.params.release_seconds, self.sample_rate);
            }
        }
    }

    fn release_all(&mut self) {
        for voice in &mut self.voices {
            voice.release(self.params.release_seconds, self.sample_rate);
        }
    }
}

pub fn midi_note_to_hz(note: u8) -> f32 {
    440.0 * 2.0_f32.powf((f32::from(note) - 69.0) / 12.0)
}

fn soft_clip(sample: f32) -> f32 {
    sample.tanh().clamp(-1.0, 1.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn max_abs(samples: &[f32]) -> f32 {
        samples
            .iter()
            .fold(0.0_f32, |acc, sample| acc.max(sample.abs()))
    }

    #[test]
    fn command_schema_round_trips_note_on() {
        let json = r#"{"type":"note_on","note":60,"velocity":96,"channel":1}"#;
        let command: SynthCommand = serde_json::from_str(json).expect("valid command");

        assert_eq!(
            command,
            SynthCommand::NoteOn {
                note: 60,
                velocity: 96,
                channel: 1
            }
        );
        assert_eq!(
            serde_json::to_string(&command).expect("serialized"),
            r#"{"type":"note_on","note":60,"velocity":96,"channel":1}"#
        );
    }

    #[test]
    fn midi_note_frequency_mapping_is_stable() {
        assert!((midi_note_to_hz(69) - 440.0).abs() < 0.001);
        assert!((midi_note_to_hz(60) - 261.62558).abs() < 0.01);
    }

    #[test]
    fn offline_render_is_non_silent_and_bounded() {
        let mut engine = SynthEngine::new(48_000.0);
        engine.handle_command(SynthCommand::NoteOn {
            note: 69,
            velocity: 110,
            channel: 1,
        });

        let samples = engine.render_offline(0.1);

        assert!(max_abs(&samples) > 0.001);
        assert!(samples.iter().all(|sample| sample.is_finite()));
        assert!(samples.iter().all(|sample| sample.abs() <= 1.0));
    }

    #[test]
    fn note_off_releases_to_silence() {
        let mut engine = SynthEngine::new(48_000.0);
        engine.handle_command(SynthCommand::SetParam {
            name: "release_seconds".to_owned(),
            value: 0.08,
        });
        engine.handle_command(SynthCommand::NoteOn {
            note: 60,
            velocity: 120,
            channel: 1,
        });

        let held = engine.render_offline(0.04);
        engine.handle_command(SynthCommand::NoteOff {
            note: 60,
            channel: 1,
        });
        let released = engine.render_offline(0.2);

        assert!(max_abs(&held) > 0.001);
        assert!(max_abs(&released[..1_000]) > 0.001);
        assert!(max_abs(&released[released.len() - 1_000..]) < 0.001);
    }

    #[test]
    fn voice_pool_respects_polyphony_limit() {
        let mut engine = SynthEngine::with_polyphony(48_000.0, 3);
        for note in 60..66 {
            engine.handle_command(SynthCommand::NoteOn {
                note,
                velocity: 96,
                channel: 1,
            });
        }

        assert_eq!(engine.active_voice_count(), 3);
    }
}
