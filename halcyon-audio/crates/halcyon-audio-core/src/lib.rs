use serde::{Deserialize, Serialize};
use std::f32::consts::PI;

pub const DEFAULT_POLYPHONY: usize = 8;
const OUTPUT_CEILING: f32 = 0.88;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum SynthCommand {
    NoteOn {
        note: u8,
        velocity: u8,
        #[serde(default = "default_channel")]
        channel: u8,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        voice_id: Option<u64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        command_id: Option<u64>,
    },
    NoteOff {
        note: u8,
        #[serde(default = "default_channel")]
        channel: u8,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        voice_id: Option<u64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        command_id: Option<u64>,
    },
    AllNotesOff {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        command_id: Option<u64>,
    },
    SetParam {
        name: String,
        value: f32,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        command_id: Option<u64>,
    },
    Shutdown {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        command_id: Option<u64>,
    },
}

impl SynthCommand {
    pub fn command_id(&self) -> Option<u64> {
        match self {
            Self::NoteOn { command_id, .. }
            | Self::NoteOff { command_id, .. }
            | Self::AllNotesOff { command_id }
            | Self::SetParam { command_id, .. }
            | Self::Shutdown { command_id } => *command_id,
        }
    }

    pub fn is_safety(&self) -> bool {
        matches!(
            self,
            Self::NoteOn { velocity: 0, .. }
                | Self::NoteOff { .. }
                | Self::AllNotesOff { .. }
                | Self::Shutdown { .. }
        )
    }

    pub fn validate(&self) -> Result<(), &'static str> {
        match self {
            Self::NoteOn {
                note,
                velocity,
                channel,
                voice_id,
                ..
            } => {
                validate_midi_note(*note)?;
                if *velocity > 127 {
                    return Err("MIDI velocity must be in 0..=127");
                }
                validate_midi_channel(*channel)?;
                validate_voice_id(*voice_id)
            }
            Self::NoteOff {
                note,
                channel,
                voice_id,
                ..
            } => {
                validate_midi_note(*note)?;
                validate_midi_channel(*channel)?;
                validate_voice_id(*voice_id)
            }
            Self::AllNotesOff { .. } | Self::SetParam { .. } | Self::Shutdown { .. } => Ok(()),
        }
    }
}

fn validate_midi_note(note: u8) -> Result<(), &'static str> {
    if note <= 127 {
        Ok(())
    } else {
        Err("MIDI note must be in 0..=127")
    }
}

fn validate_midi_channel(channel: u8) -> Result<(), &'static str> {
    if (1..=16).contains(&channel) {
        Ok(())
    } else {
        Err("MIDI channel must be in 1..=16")
    }
}

fn validate_voice_id(voice_id: Option<u64>) -> Result<(), &'static str> {
    if voice_id == Some(0) {
        Err("voice_id must be nonzero when present")
    } else {
        Ok(())
    }
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
    pub drive: f32,
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
            drive: 1.0,
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
            "drive" => self.drive = value.clamp(1.0, 8.0),
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
    voice_id: Option<u64>,
    frequency_hz: f32,
    velocity_gain: f32,
    phase: f32,
    filter_state: f32,
    envelope: Envelope,
}

impl Voice {
    fn new(note: u8, velocity: u8, channel: u8, voice_id: Option<u64>) -> Self {
        Self {
            note,
            channel,
            voice_id,
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
                voice_id,
                ..
            } => {
                if velocity == 0 {
                    self.release_note(note, channel, voice_id);
                } else {
                    self.start_note(note, velocity, channel, voice_id);
                }
                false
            }
            SynthCommand::NoteOff {
                note,
                channel,
                voice_id,
                ..
            } => {
                self.release_note(note, channel, voice_id);
                false
            }
            SynthCommand::AllNotesOff { .. } => {
                self.release_all();
                false
            }
            SynthCommand::SetParam { name, value, .. } => {
                self.params.set(&name, value);
                false
            }
            SynthCommand::Shutdown { .. } => {
                self.voices.clear();
                true
            }
        }
    }

    pub fn render_sample(&mut self) -> f32 {
        let mut sample = 0.0;
        let params = self.params;
        let sample_rate = self.sample_rate;
        let voice_count = self.voices.len().max(1) as f32;

        for voice in &mut self.voices {
            sample += voice.render(params, sample_rate);
        }
        self.voices.retain(|voice| !voice.is_done());

        let polyphony_gain = voice_count.sqrt().recip();
        soft_clip(sample * polyphony_gain * self.params.master_gain * self.params.drive)
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

    fn start_note(&mut self, note: u8, velocity: u8, channel: u8, voice_id: Option<u64>) {
        if voice_id.is_some() && self.voices.iter().any(|voice| voice.voice_id == voice_id) {
            return;
        }
        self.voices
            .retain(|voice| !(voice.note == note && voice.channel == channel && voice.is_done()));
        if self.voices.len() >= self.polyphony {
            self.voices.remove(0);
        }
        self.voices
            .push(Voice::new(note, velocity, channel, voice_id));
    }

    fn release_note(&mut self, note: u8, channel: u8, voice_id: Option<u64>) {
        for voice in &mut self.voices {
            let identity_matches = match voice_id {
                Some(expected) => voice.voice_id == Some(expected),
                None => true,
            };
            if identity_matches && voice.note == note && voice.channel == channel {
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
    (sample.tanh() * OUTPUT_CEILING).clamp(-OUTPUT_CEILING, OUTPUT_CEILING)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn max_abs(samples: &[f32]) -> f32 {
        samples
            .iter()
            .fold(0.0_f32, |acc, sample| acc.max(sample.abs()))
    }

    fn rms(samples: &[f32]) -> f32 {
        let sum = samples
            .iter()
            .map(|sample| sample * sample)
            .fold(0.0_f32, |acc, value| acc + value);
        (sum / samples.len().max(1) as f32).sqrt()
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
                channel: 1,
                voice_id: None,
                command_id: None,
            }
        );
        assert_eq!(
            serde_json::to_string(&command).expect("serialized"),
            r#"{"type":"note_on","note":60,"velocity":96,"channel":1}"#
        );
    }

    #[test]
    fn command_validation_accepts_midi_boundaries_and_legacy_voice_identity() {
        let lower = SynthCommand::NoteOn {
            note: 0,
            velocity: 0,
            channel: 1,
            voice_id: None,
            command_id: None,
        };
        let upper = SynthCommand::NoteOn {
            note: 127,
            velocity: 127,
            channel: 16,
            voice_id: Some(1),
            command_id: Some(9),
        };

        assert_eq!(lower.validate(), Ok(()));
        assert_eq!(upper.validate(), Ok(()));
    }

    #[test]
    fn command_validation_rejects_invalid_midi_and_zero_voice_boundaries() {
        let invalid = [
            (
                SynthCommand::NoteOn {
                    note: 128,
                    velocity: 100,
                    channel: 1,
                    voice_id: Some(1),
                    command_id: None,
                },
                "MIDI note must be in 0..=127",
            ),
            (
                SynthCommand::NoteOn {
                    note: 60,
                    velocity: 128,
                    channel: 1,
                    voice_id: Some(1),
                    command_id: None,
                },
                "MIDI velocity must be in 0..=127",
            ),
            (
                SynthCommand::NoteOff {
                    note: 60,
                    channel: 0,
                    voice_id: Some(1),
                    command_id: None,
                },
                "MIDI channel must be in 1..=16",
            ),
            (
                SynthCommand::NoteOff {
                    note: 60,
                    channel: 17,
                    voice_id: Some(1),
                    command_id: None,
                },
                "MIDI channel must be in 1..=16",
            ),
            (
                SynthCommand::NoteOn {
                    note: 60,
                    velocity: 100,
                    channel: 1,
                    voice_id: Some(0),
                    command_id: None,
                },
                "voice_id must be nonzero when present",
            ),
        ];

        for (command, expected) in invalid {
            assert_eq!(command.validate(), Err(expected));
        }
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
            voice_id: None,
            command_id: None,
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
            command_id: None,
        });
        engine.handle_command(SynthCommand::NoteOn {
            note: 60,
            velocity: 120,
            channel: 1,
            voice_id: None,
            command_id: None,
        });

        let held = engine.render_offline(0.04);
        engine.handle_command(SynthCommand::NoteOff {
            note: 60,
            channel: 1,
            voice_id: None,
            command_id: None,
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
                voice_id: None,
                command_id: None,
            });
        }

        assert_eq!(engine.active_voice_count(), 3);
    }

    #[test]
    fn drive_param_clamps_and_keeps_output_bounded() {
        let mut engine = SynthEngine::new(48_000.0);
        engine.handle_command(SynthCommand::SetParam {
            name: "drive".to_owned(),
            value: 99.0,
            command_id: None,
        });
        assert_eq!(engine.params().drive, 8.0);

        engine.handle_command(SynthCommand::NoteOn {
            note: 64,
            velocity: 127,
            channel: 1,
            voice_id: None,
            command_id: None,
        });
        let samples = engine.render_offline(0.1);

        assert!(max_abs(&samples) > 0.001);
        assert!(samples.iter().all(|sample| sample.is_finite()));
        assert!(samples.iter().all(|sample| sample.abs() <= 1.0));
        assert!(max_abs(&samples) <= OUTPUT_CEILING);
    }

    #[test]
    fn chords_keep_reasonable_headphone_level_against_single_notes() {
        let mut single = SynthEngine::new(48_000.0);
        single.handle_command(SynthCommand::NoteOn {
            note: 60,
            velocity: 120,
            channel: 1,
            voice_id: None,
            command_id: None,
        });
        let single_samples = single.render_offline(0.2);

        let mut chord = SynthEngine::new(48_000.0);
        for note in [60, 64, 67, 71] {
            chord.handle_command(SynthCommand::NoteOn {
                note,
                velocity: 120,
                channel: 1,
                voice_id: None,
                command_id: None,
            });
        }
        let chord_samples = chord.render_offline(0.2);

        assert!(rms(&single_samples) > 0.001);
        assert!(rms(&chord_samples) < rms(&single_samples) * 2.2);
    }

    #[test]
    fn command_schema_round_trips_optional_voice_and_command_ids() {
        let json = r#"{"type":"note_off","note":60,"channel":1,"voice_id":91,"command_id":7}"#;
        let command: SynthCommand = serde_json::from_str(json).expect("valid command");

        assert_eq!(command.command_id(), Some(7));
        assert!(command.is_safety());
        assert_eq!(serde_json::to_string(&command).expect("serialized"), json);
    }

    #[test]
    fn velocity_zero_note_on_uses_safety_admission_classification() {
        let command: SynthCommand = serde_json::from_str(
            r#"{"type":"note_on","note":60,"velocity":0,"channel":1,"voice_id":91}"#,
        )
        .expect("valid command");

        assert!(command.is_safety());
    }

    #[test]
    fn voice_ids_release_same_note_instances_independently() {
        let mut engine = SynthEngine::new(48_000.0);
        engine.handle_command(SynthCommand::SetParam {
            name: "release_seconds".to_owned(),
            value: 0.0,
            command_id: None,
        });
        for voice_id in [100, 200] {
            engine.handle_command(SynthCommand::NoteOn {
                note: 60,
                velocity: 100,
                channel: 1,
                voice_id: Some(voice_id),
                command_id: None,
            });
        }
        assert_eq!(engine.active_voice_count(), 2);

        engine.handle_command(SynthCommand::NoteOff {
            note: 60,
            channel: 1,
            voice_id: Some(100),
            command_id: None,
        });
        engine.render_sample();
        assert_eq!(engine.active_voice_count(), 1);

        engine.handle_command(SynthCommand::NoteOff {
            note: 60,
            channel: 1,
            voice_id: Some(999),
            command_id: None,
        });
        engine.render_sample();
        assert_eq!(engine.active_voice_count(), 1);
    }

    #[test]
    fn absent_voice_id_keeps_legacy_note_channel_release() {
        let mut engine = SynthEngine::new(48_000.0);
        engine.handle_command(SynthCommand::SetParam {
            name: "release_seconds".to_owned(),
            value: 0.0,
            command_id: None,
        });
        for voice_id in [Some(10), Some(20)] {
            engine.handle_command(SynthCommand::NoteOn {
                note: 62,
                velocity: 100,
                channel: 1,
                voice_id,
                command_id: None,
            });
        }

        engine.handle_command(SynthCommand::NoteOff {
            note: 62,
            channel: 1,
            voice_id: None,
            command_id: None,
        });
        engine.render_sample();
        assert_eq!(engine.active_voice_count(), 0);
    }
}
