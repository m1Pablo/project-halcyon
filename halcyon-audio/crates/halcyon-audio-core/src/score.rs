//! Offline score rendering for the Halcyon synth engine.
//!
//! The realtime sidecar (`halcyon-audio-sidecar`) needs a CPAL output
//! device, so it cannot make sound on a headless machine. The DSP engine
//! itself has no device dependency, and this module gives it the missing
//! piece: a sample-accurate timeline of notes that can be rendered to a
//! buffer (and written as a WAV file) anywhere, with no MIDI hardware and
//! no audio device.
//!
//! A score is plain JSON, for example:
//!
//! ```json
//! {
//!   "sample_rate": 44100,
//!   "params": {"master_gain": 0.32, "saw_square_blend": 0.2},
//!   "notes": [
//!     {"note": 72, "velocity": 100, "start_seconds": 0.0, "duration_seconds": 0.3},
//!     {"note": 76, "velocity": 100, "start_seconds": 0.3, "duration_seconds": 0.3}
//!   ]
//! }
//! ```
//!
//! `params` uses the same DSP names as `SynthCommand::SetParam`
//! (`master_gain`, `cutoff_hz`, ...), i.e. the values the Python side
//! produces from a preset through `halcyon/synth_params.py`.

use crate::{SynthCommand, SynthEngine};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::io::{self, Write};

fn default_sample_rate() -> f32 {
    44_100.0
}

fn default_channel() -> u8 {
    1
}

fn default_velocity() -> u8 {
    100
}

/// One scheduled note in a [`Score`].
#[derive(Clone, Debug, Deserialize, PartialEq)]
pub struct ScoreNote {
    pub note: u8,
    #[serde(default = "default_velocity")]
    pub velocity: u8,
    #[serde(default = "default_channel")]
    pub channel: u8,
    pub start_seconds: f32,
    pub duration_seconds: f32,
}

/// A timed sequence of notes plus the synth patch to render them with.
#[derive(Clone, Debug, Deserialize)]
pub struct Score {
    #[serde(default = "default_sample_rate")]
    pub sample_rate: f32,
    /// DSP parameter overrides, applied before the first note.
    #[serde(default)]
    pub params: BTreeMap<String, f32>,
    pub notes: Vec<ScoreNote>,
    /// Extra silence rendered after the last note ends, so release tails
    /// are not cut off. Defaults to the patch's release time plus a
    /// small margin when not set.
    #[serde(default)]
    pub tail_seconds: Option<f32>,
}

impl Score {
    fn end_seconds(&self) -> f32 {
        self.notes
            .iter()
            .map(|note| note.start_seconds.max(0.0) + note.duration_seconds.max(0.0))
            .fold(0.0_f32, f32::max)
    }
}

/// Render a score with the exact `SynthEngine` used by the realtime
/// sidecar. Note on/off commands are applied sample-accurately.
pub fn render_score(score: &Score) -> Vec<f32> {
    let sample_rate = score.sample_rate.max(1.0);
    let mut engine = SynthEngine::new(sample_rate);
    for (name, value) in &score.params {
        engine.handle_command(SynthCommand::SetParam {
            name: name.clone(),
            value: *value,
        });
    }

    let tail = score
        .tail_seconds
        .unwrap_or_else(|| engine.params().release_seconds + 0.05);
    let total_seconds = score.end_seconds() + tail.max(0.0);
    let frame_count = (total_seconds * sample_rate).round() as usize;

    // (sample index, is_off, command); note-offs sort before note-ons at
    // the same sample so a note can be retriggered cleanly.
    let mut events: Vec<(usize, u8, SynthCommand)> = Vec::with_capacity(score.notes.len() * 2);
    for note in &score.notes {
        let on_at = (note.start_seconds.max(0.0) * sample_rate).round() as usize;
        let off_at = on_at + (note.duration_seconds.max(0.0) * sample_rate).round() as usize;
        events.push((
            on_at,
            1,
            SynthCommand::NoteOn {
                note: note.note,
                velocity: note.velocity,
                channel: note.channel,
            },
        ));
        events.push((
            off_at.min(frame_count),
            0,
            SynthCommand::NoteOff {
                note: note.note,
                channel: note.channel,
            },
        ));
    }
    events.sort_by_key(|(at, kind, _)| (*at, *kind));

    let mut output = Vec::with_capacity(frame_count);
    let mut next_event = 0;
    for frame in 0..frame_count {
        while next_event < events.len() && events[next_event].0 <= frame {
            engine.handle_command(events[next_event].2.clone());
            next_event += 1;
        }
        output.push(engine.render_sample());
    }
    output
}

/// Write mono samples as a 16-bit PCM WAV file.
pub fn write_wav<W: Write>(writer: &mut W, samples: &[f32], sample_rate: f32) -> io::Result<()> {
    let sample_rate = sample_rate.max(1.0) as u32;
    let data_len = (samples.len() * 2) as u32;
    writer.write_all(b"RIFF")?;
    writer.write_all(&(36 + data_len).to_le_bytes())?;
    writer.write_all(b"WAVE")?;
    writer.write_all(b"fmt ")?;
    writer.write_all(&16_u32.to_le_bytes())?; // fmt chunk size
    writer.write_all(&1_u16.to_le_bytes())?; // PCM
    writer.write_all(&1_u16.to_le_bytes())?; // mono
    writer.write_all(&sample_rate.to_le_bytes())?;
    writer.write_all(&(sample_rate * 2).to_le_bytes())?; // byte rate
    writer.write_all(&2_u16.to_le_bytes())?; // block align
    writer.write_all(&16_u16.to_le_bytes())?; // bits per sample
    writer.write_all(b"data")?;
    writer.write_all(&data_len.to_le_bytes())?;
    for sample in samples {
        let value = (sample.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
        writer.write_all(&value.to_le_bytes())?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn max_abs(samples: &[f32]) -> f32 {
        samples.iter().fold(0.0_f32, |acc, s| acc.max(s.abs()))
    }

    fn one_note_score() -> Score {
        Score {
            sample_rate: 44_100.0,
            params: BTreeMap::new(),
            notes: vec![ScoreNote {
                note: 69,
                velocity: 110,
                channel: 1,
                start_seconds: 0.0,
                duration_seconds: 0.2,
            }],
            tail_seconds: Some(0.3),
        }
    }

    #[test]
    fn empty_score_renders_silence() {
        let score = Score {
            sample_rate: 44_100.0,
            params: BTreeMap::new(),
            notes: vec![],
            tail_seconds: Some(0.05),
        };
        let samples = render_score(&score);
        assert!(!samples.is_empty());
        assert_eq!(max_abs(&samples), 0.0);
    }

    #[test]
    fn scored_note_is_non_silent_bounded_and_releases() {
        let samples = render_score(&one_note_score());
        assert!(max_abs(&samples) > 0.001);
        assert!(samples.iter().all(|s| s.is_finite() && s.abs() <= 1.0));
        // The tail after note-off + release must fall silent.
        assert!(max_abs(&samples[samples.len() - 1_000..]) < 0.001);
    }

    #[test]
    fn score_parses_from_json_with_defaults() {
        let score: Score = serde_json::from_str(
            r#"{"notes":[{"note":60,"start_seconds":0.0,"duration_seconds":0.1}]}"#,
        )
        .expect("valid score json");
        assert_eq!(score.sample_rate, 44_100.0);
        assert_eq!(score.notes[0].velocity, 100);
        assert_eq!(score.notes[0].channel, 1);
    }

    #[test]
    fn wav_header_and_data_sizes_match() {
        let samples = vec![0.0_f32; 441];
        let mut bytes: Vec<u8> = Vec::new();
        write_wav(&mut bytes, &samples, 44_100.0).expect("write wav");
        assert_eq!(&bytes[0..4], b"RIFF");
        assert_eq!(&bytes[8..12], b"WAVE");
        assert_eq!(bytes.len(), 44 + samples.len() * 2);
    }
}
