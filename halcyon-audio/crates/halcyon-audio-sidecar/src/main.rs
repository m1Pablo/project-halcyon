use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use halcyon_audio_core::{SynthCommand, SynthEngine};
use rtrb::{Consumer, Producer, PushError, RingBuffer};
use std::error::Error;
use std::io::{self, BufRead};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread;
use std::time::{Duration, Instant};

mod viz;

const COMMAND_QUEUE_SIZE: usize = 1024;
const SAFETY_QUEUE_RESERVE: usize = 64;
const PRODUCTION_SAFETY_POLICY: SafetyAdmissionPolicy = SafetyAdmissionPolicy {
    max_attempts: 500,
    timeout: Duration::from_millis(500),
    retry_delay: Duration::from_millis(1),
};

#[derive(Clone, Copy, Debug)]
struct SafetyAdmissionPolicy {
    max_attempts: usize,
    timeout: Duration,
    retry_delay: Duration,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum SafetyAdmissionFailureKind {
    AudioStopped,
    Timeout,
}

#[derive(Debug)]
struct SafetyAdmissionFailure {
    command: SynthCommand,
    kind: SafetyAdmissionFailureKind,
}

impl SafetyAdmissionFailure {
    fn code(&self) -> &'static str {
        match self.kind {
            SafetyAdmissionFailureKind::AudioStopped => "AUDIO_SAFETY_ADMISSION_STOPPED",
            SafetyAdmissionFailureKind::Timeout => "AUDIO_SAFETY_ADMISSION_TIMEOUT",
        }
    }

    fn message(&self) -> &'static str {
        match self.kind {
            SafetyAdmissionFailureKind::AudioStopped => {
                "audio stopped before safety command admission"
            }
            SafetyAdmissionFailureKind::Timeout => {
                "safety command admission timed out while the audio queue was stalled"
            }
        }
    }
}

#[derive(Debug)]
enum CommandAdmissionFailure {
    Normal(SynthCommand),
    Safety(SafetyAdmissionFailure),
}

#[derive(Debug)]
struct CommandParseFailure {
    command_id: Option<u64>,
    message: String,
}

impl CommandParseFailure {
    fn code(&self) -> &'static str {
        "INVALID_AUDIO_COMMAND"
    }
}

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
    let (viz_producer, viz_handle) =
        viz::spawn_viz_thread(viz::VizConfig::from_env(), Arc::clone(&running));

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
            viz_producer,
            Arc::clone(&running),
        )?,
        cpal::SampleFormat::I16 => build_stream::<i16>(
            &device,
            &stream_config,
            sample_rate,
            channels,
            consumer,
            viz_producer,
            Arc::clone(&running),
        )?,
        cpal::SampleFormat::U16 => build_stream::<u16>(
            &device,
            &stream_config,
            sample_rate,
            channels,
            consumer,
            viz_producer,
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
    let _ = viz_handle.join();
    Ok(())
}

fn spawn_stdin_thread(
    mut producer: Producer<SynthCommand>,
    running: Arc<AtomicBool>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let stdin = io::stdin();
        let mut shutdown_admitted = false;
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

            let command = match parse_command_line(&line) {
                Ok(command) => command,
                Err(failure) => {
                    reject_parse_failure(&failure, &running);
                    break;
                }
            };
            let command_id = command.command_id();
            let is_safety = command.is_safety();
            let is_shutdown = matches!(command, SynthCommand::Shutdown { .. });
            let admission = if is_safety {
                admit_safety(&mut producer, command, &running, PRODUCTION_SAFETY_POLICY)
                    .map_err(CommandAdmissionFailure::Safety)
            } else {
                admit_normal(&mut producer, command, SAFETY_QUEUE_RESERVE)
                    .map_err(CommandAdmissionFailure::Normal)
            };

            match admission {
                Ok(()) => {
                    print_admission(command_id, "accepted", is_safety, None, None);
                    shutdown_admitted = is_shutdown;
                }
                Err(failure) => match failure {
                    CommandAdmissionFailure::Normal(_command) => {
                        print_admission(
                            command_id,
                            "rejected",
                            false,
                            Some("AUDIO_BACKPRESSURE"),
                            Some("normal command queue reached its reserved safety boundary"),
                        );
                    }
                    CommandAdmissionFailure::Safety(failure) => {
                        print_admission(
                            failure.command.command_id().or(command_id),
                            "rejected",
                            true,
                            Some(failure.code()),
                            Some(failure.message()),
                        );
                        running.store(false, Ordering::SeqCst);
                        break;
                    }
                },
            }
            if is_shutdown {
                break;
            }
        }

        if running.load(Ordering::SeqCst) && !shutdown_admitted {
            let _ = admit_implicit_shutdown(&mut producer, &running, PRODUCTION_SAFETY_POLICY);
        }
    })
}

fn parse_command_line(line: &str) -> Result<SynthCommand, CommandParseFailure> {
    let value: serde_json::Value =
        serde_json::from_str(line).map_err(|error| CommandParseFailure {
            command_id: None,
            message: error.to_string(),
        })?;
    let command_id = value.get("command_id").and_then(serde_json::Value::as_u64);
    let command: SynthCommand =
        serde_json::from_value(value).map_err(|error| CommandParseFailure {
            command_id,
            message: error.to_string(),
        })?;
    command.validate().map_err(|message| CommandParseFailure {
        command_id,
        message: message.to_owned(),
    })?;
    Ok(command)
}

fn reject_parse_failure(failure: &CommandParseFailure, running: &AtomicBool) {
    print_admission(
        failure.command_id,
        "rejected",
        false,
        Some(failure.code()),
        Some(&failure.message),
    );
    running.store(false, Ordering::SeqCst);
}

fn admit_normal(
    producer: &mut Producer<SynthCommand>,
    command: SynthCommand,
    safety_reserve: usize,
) -> Result<(), SynthCommand> {
    if producer.slots() <= safety_reserve {
        return Err(command);
    }
    producer
        .push(command)
        .map_err(|PushError::Full(command)| command)
}

fn admit_safety(
    producer: &mut Producer<SynthCommand>,
    mut command: SynthCommand,
    running: &AtomicBool,
    policy: SafetyAdmissionPolicy,
) -> Result<(), SafetyAdmissionFailure> {
    let started = Instant::now();
    let mut attempts = 0;
    loop {
        match producer.push(command) {
            Ok(()) => return Ok(()),
            Err(PushError::Full(returned)) => {
                command = returned;
                attempts += 1;
                if !running.load(Ordering::SeqCst) {
                    return Err(SafetyAdmissionFailure {
                        command,
                        kind: SafetyAdmissionFailureKind::AudioStopped,
                    });
                }
                let elapsed = started.elapsed();
                if attempts >= policy.max_attempts || elapsed >= policy.timeout {
                    running.store(false, Ordering::SeqCst);
                    return Err(SafetyAdmissionFailure {
                        command,
                        kind: SafetyAdmissionFailureKind::Timeout,
                    });
                }
                let remaining = policy.timeout.saturating_sub(elapsed);
                thread::sleep(policy.retry_delay.min(remaining));
            }
        }
    }
}

fn admit_implicit_shutdown(
    producer: &mut Producer<SynthCommand>,
    running: &AtomicBool,
    policy: SafetyAdmissionPolicy,
) -> Result<(), SafetyAdmissionFailure> {
    let result = admit_safety(
        producer,
        SynthCommand::Shutdown { command_id: None },
        running,
        policy,
    );
    if let Err(failure) = &result {
        print_admission(
            None,
            "rejected",
            true,
            Some(failure.code()),
            Some(failure.message()),
        );
        running.store(false, Ordering::SeqCst);
    }
    result
}

fn print_admission(
    command_id: Option<u64>,
    status: &str,
    is_safety: bool,
    code: Option<&str>,
    message: Option<&str>,
) {
    let Some(command_id) = command_id else {
        if status != "accepted" {
            println!(
                "{}",
                serde_json::json!({
                    "type": "error",
                    "code": code,
                    "message": message.unwrap_or("audio command admission failed"),
                })
            );
        }
        return;
    };
    println!(
        "{}",
        serde_json::json!({
            "type": "admission",
            "command_id": command_id,
            "status": status,
            "queue": if is_safety { "safety" } else { "normal" },
            "code": code,
            "message": message,
        })
    );
}

fn build_stream<T>(
    device: &cpal::Device,
    config: &cpal::StreamConfig,
    sample_rate: f32,
    channels: u16,
    mut consumer: Consumer<SynthCommand>,
    mut viz_producer: Producer<f32>,
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
                &mut viz_producer,
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

fn write_output<T>(
    output: &mut [T],
    channels: usize,
    engine: &mut SynthEngine,
    viz_producer: &mut Producer<f32>,
    running: bool,
) where
    T: cpal::Sample + cpal::FromSample<f32>,
{
    for frame in output.chunks_mut(channels) {
        let sample = if running { engine.render_sample() } else { 0.0 };
        let _ = viz_producer.push(sample);
        let converted = T::from_sample(sample);
        for channel in frame {
            *channel = converted;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn note_on(command_id: u64) -> SynthCommand {
        SynthCommand::NoteOn {
            note: 60,
            velocity: 100,
            channel: 1,
            voice_id: Some(command_id),
            command_id: Some(command_id),
        }
    }

    fn immediate_timeout_policy() -> SafetyAdmissionPolicy {
        SafetyAdmissionPolicy {
            max_attempts: 1,
            timeout: Duration::ZERO,
            retry_delay: Duration::ZERO,
        }
    }

    #[test]
    fn normal_admission_preserves_capacity_for_fifo_safety_command() {
        let (mut producer, mut consumer) = RingBuffer::new(4);
        for command_id in 1..=3 {
            assert!(admit_normal(&mut producer, note_on(command_id), 1).is_ok());
        }
        assert!(admit_normal(&mut producer, note_on(4), 1).is_err());

        let running = AtomicBool::new(true);
        let safety = SynthCommand::NoteOff {
            note: 60,
            channel: 1,
            voice_id: Some(1),
            command_id: Some(5),
        };
        assert!(admit_safety(&mut producer, safety, &running, PRODUCTION_SAFETY_POLICY,).is_ok());

        let ids: Vec<Option<u64>> = std::iter::from_fn(|| consumer.pop().ok())
            .map(|command| command.command_id())
            .collect();
        assert_eq!(ids, vec![Some(1), Some(2), Some(3), Some(5)]);
    }

    #[test]
    fn safety_admission_failure_returns_command_when_audio_has_stopped() {
        let (mut producer, _consumer) = RingBuffer::new(1);
        producer.push(note_on(1)).expect("initial command admitted");
        let running = AtomicBool::new(false);
        let safety = SynthCommand::AllNotesOff {
            command_id: Some(2),
        };

        let failure = admit_safety(&mut producer, safety, &running, immediate_timeout_policy())
            .expect_err("visible failure");
        assert_eq!(failure.command.command_id(), Some(2));
        assert_eq!(failure.kind, SafetyAdmissionFailureKind::AudioStopped);
    }

    #[test]
    fn safety_admission_times_out_if_running_callback_stalls_on_full_ring() {
        let (mut producer, _consumer) = RingBuffer::new(1);
        producer.push(note_on(1)).expect("initial command admitted");
        let running = AtomicBool::new(true);
        let safety = SynthCommand::NoteOff {
            note: 60,
            channel: 1,
            voice_id: Some(1),
            command_id: Some(2),
        };

        let failure = admit_safety(&mut producer, safety, &running, immediate_timeout_policy())
            .expect_err("bounded timeout");

        assert_eq!(failure.command.command_id(), Some(2));
        assert_eq!(failure.kind, SafetyAdmissionFailureKind::Timeout);
        assert_eq!(failure.code(), "AUDIO_SAFETY_ADMISSION_TIMEOUT");
        assert!(!running.load(Ordering::SeqCst));
    }

    #[test]
    fn implicit_shutdown_failure_is_visible_and_forces_terminal_state() {
        let (mut producer, _consumer) = RingBuffer::new(1);
        producer.push(note_on(1)).expect("initial command admitted");
        let running = AtomicBool::new(true);

        let failure = admit_implicit_shutdown(&mut producer, &running, immediate_timeout_policy())
            .expect_err("implicit shutdown must not fail silently");

        assert!(matches!(failure.command, SynthCommand::Shutdown { .. }));
        assert_eq!(failure.kind, SafetyAdmissionFailureKind::Timeout);
        assert!(!running.load(Ordering::SeqCst));
    }

    #[test]
    fn protocol_validation_accepts_exact_midi_boundaries() {
        let lower = parse_command_line(
            r#"{"type":"note_on","note":0,"velocity":0,"channel":1,"command_id":40}"#,
        )
        .expect("lower boundary");
        let upper = parse_command_line(
            r#"{"type":"note_on","note":127,"velocity":127,"channel":16,"voice_id":1,"command_id":41}"#,
        )
        .expect("upper boundary");

        assert_eq!(lower.command_id(), Some(40));
        assert_eq!(upper.command_id(), Some(41));
    }

    #[test]
    fn protocol_validation_rejects_midi_and_voice_boundaries_with_command_ids() {
        let invalid = [
            (
                r#"{"type":"note_on","note":128,"velocity":100,"channel":1,"command_id":44}"#,
                44,
                "MIDI note",
            ),
            (
                r#"{"type":"note_on","note":60,"velocity":128,"channel":1,"command_id":45}"#,
                45,
                "MIDI velocity",
            ),
            (
                r#"{"type":"note_off","note":60,"channel":0,"command_id":46}"#,
                46,
                "MIDI channel",
            ),
            (
                r#"{"type":"note_off","note":60,"channel":17,"command_id":47}"#,
                47,
                "MIDI channel",
            ),
            (
                r#"{"type":"note_on","note":60,"velocity":100,"channel":1,"voice_id":0,"command_id":48}"#,
                48,
                "voice_id",
            ),
        ];

        for (line, command_id, expected_message) in invalid {
            let failure = parse_command_line(line).expect_err("invalid protocol boundary");
            assert_eq!(failure.command_id, Some(command_id));
            assert_eq!(failure.code(), "INVALID_AUDIO_COMMAND");
            assert!(failure.message.contains(expected_message));
        }

        let failure = parse_command_line(invalid[0].0).expect_err("terminal boundary failure");
        let running = AtomicBool::new(true);
        reject_parse_failure(&failure, &running);
        assert!(!running.load(Ordering::SeqCst));
    }

    #[test]
    fn malformed_json_parse_failure_is_terminal_without_recoverable_id() {
        let failure =
            parse_command_line(r#"{"type":"note_on","command_id":45"#).expect_err("malformed JSON");
        let running = AtomicBool::new(true);

        reject_parse_failure(&failure, &running);

        assert_eq!(failure.command_id, None);
        assert!(!running.load(Ordering::SeqCst));
    }
}
