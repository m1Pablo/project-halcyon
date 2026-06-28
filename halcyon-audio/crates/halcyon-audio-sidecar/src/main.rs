use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use halcyon_audio_core::{SynthCommand, SynthEngine};
use rtrb::{Consumer, Producer, RingBuffer};
use std::error::Error;
use std::io::{self, BufRead};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::Duration;

const COMMAND_QUEUE_SIZE: usize = 1024;

fn main() {
    if let Err(error) = run() {
        println!(
            "{}",
            serde_json::json!({
                "type": "error",
                "message": error.to_string(),
            })
        );
        eprintln!("halcyon-audio-sidecar: {error}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let (producer, consumer) = RingBuffer::<SynthCommand>::new(COMMAND_QUEUE_SIZE);
    let running = Arc::new(AtomicBool::new(true));
    let stdin_handle = spawn_stdin_thread(producer, Arc::clone(&running));

    let host = cpal::default_host();
    let device = host
        .default_output_device()
        .ok_or("no default output device")?;
    let supported_config = device.default_output_config()?;
    let sample_rate = supported_config.sample_rate().0 as f32;
    let channels = supported_config.channels();
    let stream_config = supported_config.config();

    let stream = match supported_config.sample_format() {
        cpal::SampleFormat::F32 => build_stream::<f32>(
            &device,
            &stream_config,
            sample_rate,
            channels,
            consumer,
            Arc::clone(&running),
        )?,
        cpal::SampleFormat::I16 => build_stream::<i16>(
            &device,
            &stream_config,
            sample_rate,
            channels,
            consumer,
            Arc::clone(&running),
        )?,
        cpal::SampleFormat::U16 => build_stream::<u16>(
            &device,
            &stream_config,
            sample_rate,
            channels,
            consumer,
            Arc::clone(&running),
        )?,
        sample_format => return Err(format!("unsupported sample format: {sample_format:?}").into()),
    };

    stream.play()?;
    println!(
        "{}",
        serde_json::json!({
            "type": "status",
            "status": "audio_ready",
            "sample_rate": sample_rate as u32,
            "channels": channels,
        })
    );

    while running.load(Ordering::SeqCst) {
        thread::sleep(Duration::from_millis(50));
    }

    drop(stream);
    let _ = stdin_handle.join();
    Ok(())
}

fn spawn_stdin_thread(
    mut producer: Producer<SynthCommand>,
    running: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let stdin = io::stdin();
        for line in stdin.lock().lines() {
            if !running.load(Ordering::SeqCst) {
                break;
            }

            let line = match line {
                Ok(line) => line,
                Err(error) => {
                    eprintln!("stdin read error: {error}");
                    break;
                }
            };
            if line.trim().is_empty() {
                continue;
            }

            let command = match serde_json::from_str::<SynthCommand>(&line) {
                Ok(command) => command,
                Err(error) => {
                    eprintln!("invalid command: {error}");
                    continue;
                }
            };
            let is_shutdown = matches!(command, SynthCommand::Shutdown);
            if producer.push(command).is_err() {
                eprintln!("audio command queue full; dropping command");
                if is_shutdown {
                    running.store(false, Ordering::SeqCst);
                    break;
                }
            }
            if is_shutdown {
                break;
            }
        }

        let _ = producer.push(SynthCommand::Shutdown);
    })
}

fn build_stream<T>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    sample_rate: f32,
    channels: u16,
    mut consumer: Consumer<SynthCommand>,
    running: Arc<AtomicBool>,
) -> Result<cpal::Stream, cpal::BuildStreamError>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let mut engine = SynthEngine::new(sample_rate);
    let channel_count = usize::from(channels);
    let audio_running = Arc::clone(&running);
    let error_running = Arc::clone(&running);

    device.build_output_stream(
        config,
        move |output: &mut [T], _: &cpal::OutputCallbackInfo| {
            while let Ok(command) = consumer.pop() {
                let shutdown = engine.handle_command(command);
                if shutdown {
                    audio_running.store(false, Ordering::SeqCst);
                }
            }

            write_output(
                output,
                channel_count,
                &mut engine,
                audio_running.load(Ordering::SeqCst),
            );
        },
        move |error| {
            eprintln!("audio stream error: {error}");
            error_running.store(false, Ordering::SeqCst);
        },
        None,
    )
}

fn write_output<T>(output: &mut [T], channels: usize, engine: &mut SynthEngine, running: bool)
where
    T: cpal::Sample + cpal::FromSample<f32>,
{
    for frame in output.chunks_mut(channels) {
        let sample = if running { engine.render_sample() } else { 0.0 };
        let converted = T::from_sample(sample);
        for channel in frame {
            *channel = converted;
        }
    }
}
