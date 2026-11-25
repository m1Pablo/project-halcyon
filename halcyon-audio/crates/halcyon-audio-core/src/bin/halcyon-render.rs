//! Render a Halcyon score JSON file to a WAV file, with no audio device.
//!
//! Usage:
//!   halcyon-render <score.json> <output.wav>
//!   halcyon-render - <output.wav>        (score JSON from stdin)
//!
//! The score format is documented in `halcyon_audio_core::score`.
//! This uses the same `SynthEngine` as the realtime CPAL sidecar, so an
//! offline render sounds like the live instrument with the same patch,
//! without needing MIDI hardware or an output device.

use halcyon_audio_core::score::{render_score, write_wav, Score};
use std::env;
use std::error::Error;
use std::fs::File;
use std::io::{self, BufWriter, Read};
use std::process::ExitCode;

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("halcyon-render: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = env::args().collect();
    if args.len() != 3 {
        return Err(format!(
            "usage: {} <score.json|-> <output.wav>",
            args.first().map(String::as_str).unwrap_or("halcyon-render")
        )
        .into());
    }

    let json = if args[1] == "-" {
        let mut buffer = String::new();
        io::stdin().read_to_string(&mut buffer)?;
        buffer
    } else {
        std::fs::read_to_string(&args[1])?
    };
    let score: Score = serde_json::from_str(&json)?;
    let sample_rate = score.sample_rate;
    let samples = render_score(&score);

    let file = File::create(&args[2])?;
    let mut writer = BufWriter::new(file);
    write_wav(&mut writer, &samples, sample_rate)?;
    println!(
        "rendered {:.2}s ({} samples at {} Hz) to {}",
        samples.len() as f32 / sample_rate.max(1.0),
        samples.len(),
        sample_rate as u32,
        args[2]
    );
    Ok(())
}
