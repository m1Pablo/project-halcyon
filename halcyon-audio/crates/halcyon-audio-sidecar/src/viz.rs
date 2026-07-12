use rtrb::{Consumer, Producer, RingBuffer};
use std::env;
use std::io::{self, Write};
use std::os::unix::net::UnixStream;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

pub const DEFAULT_SOCKET_PATH: &str = "/tmp/synth_viz.sock";
pub const DEFAULT_SAMPLE_COUNT: usize = 1024;
pub const DEFAULT_FPS: u32 = 60;
pub const FRAME_MAGIC: [u8; 4] = *b"HZV1";

const MAX_SAMPLE_COUNT: usize = 16_384;
const QUEUE_MULTIPLIER: usize = 8;
const RECONNECT_INTERVAL: Duration = Duration::from_millis(250);
const IDLE_SLEEP: Duration = Duration::from_millis(2);

#[derive(Clone, Debug)]
pub struct VizConfig {
    pub socket_path: String,
    pub sample_count: usize,
    pub frames_per_second: u32,
}

impl VizConfig {
    pub fn from_env() -> Self {
        Self {
            socket_path: env::var("HALCYON_VIZ_SOCKET")
                .unwrap_or_else(|_| DEFAULT_SOCKET_PATH.to_owned()),
            sample_count: parse_env_usize("HALCYON_VIZ_SAMPLES", DEFAULT_SAMPLE_COUNT)
                .clamp(1, MAX_SAMPLE_COUNT),
            frames_per_second: parse_env_u32("HALCYON_VIZ_FPS", DEFAULT_FPS).clamp(1, 120),
        }
    }

    fn frame_interval(&self) -> Duration {
        Duration::from_secs_f64(1.0 / f64::from(self.frames_per_second))
    }
}

pub fn spawn_viz_thread(
    config: VizConfig,
    running: Arc<AtomicBool>,
) -> (Producer<f32>, JoinHandle<()>) {
    let capacity = (config.sample_count * QUEUE_MULTIPLIER).max(1024);
    let (producer, consumer) = RingBuffer::<f32>::new(capacity);
    let handle = thread::spawn(move || run_viz_thread(config, consumer, running));
    (producer, handle)
}

pub fn encode_frame(samples: &[f32]) -> Vec<u8> {
    let mut frame = Vec::with_capacity(8 + (samples.len() * 4));
    frame.extend_from_slice(&FRAME_MAGIC);
    frame.extend_from_slice(&(samples.len() as u32).to_le_bytes());
    for sample in samples {
        frame.extend_from_slice(&sample.to_le_bytes());
    }
    frame
}

#[derive(Debug)]
struct PendingFrame {
    bytes: Vec<u8>,
    written: usize,
}

impl PendingFrame {
    fn new(bytes: Vec<u8>) -> Self {
        Self { bytes, written: 0 }
    }

    fn remaining(&self) -> &[u8] {
        &self.bytes[self.written..]
    }

    fn advance(&mut self, count: usize) {
        self.written = (self.written + count).min(self.bytes.len());
    }

    fn is_complete(&self) -> bool {
        self.written >= self.bytes.len()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FrameSendResult {
    Complete,
    Pending,
    Disconnect,
}

fn run_viz_thread(config: VizConfig, mut consumer: Consumer<f32>, running: Arc<AtomicBool>) {
    let mut window = RollingSampleWindow::new(config.sample_count);
    let mut stream: Option<UnixStream> = None;
    let mut pending_frame: Option<PendingFrame> = None;
    let mut next_connect = Instant::now();
    let mut next_send = Instant::now();
    let frame_interval = config.frame_interval();

    while running.load(Ordering::SeqCst) {
        while let Ok(sample) = consumer.pop() {
            window.push(sample);
        }

        let now = Instant::now();
        if stream.is_none() && now >= next_connect {
            stream = connect_socket(&config.socket_path);
            next_connect = now + RECONNECT_INTERVAL;
        }

        if now >= next_send {
            next_send = now + frame_interval;
            if stream.is_some() && pending_frame.is_none() {
                pending_frame = Some(PendingFrame::new(encode_frame(&window.snapshot())));
            }
        }

        if let (Some(socket), Some(frame)) = (stream.as_mut(), pending_frame.as_mut()) {
            match flush_pending_frame(socket, frame) {
                FrameSendResult::Complete => pending_frame = None,
                FrameSendResult::Pending => {}
                FrameSendResult::Disconnect => {
                    pending_frame = None;
                    stream = None;
                }
            }
        }

        thread::sleep(IDLE_SLEEP);
    }
}

fn flush_pending_frame<W: Write>(writer: &mut W, frame: &mut PendingFrame) -> FrameSendResult {
    while !frame.is_complete() {
        match writer.write(frame.remaining()) {
            Ok(0) => return FrameSendResult::Disconnect,
            Ok(written) => frame.advance(written),
            Err(error) if error.kind() == io::ErrorKind::WouldBlock => {
                return FrameSendResult::Pending;
            }
            Err(_) => return FrameSendResult::Disconnect,
        }
    }
    FrameSendResult::Complete
}

fn connect_socket(socket_path: &str) -> Option<UnixStream> {
    match UnixStream::connect(socket_path) {
        Ok(stream) => {
            if stream.set_nonblocking(true).is_ok() {
                Some(stream)
            } else {
                None
            }
        }
        Err(_) => None,
    }
}

fn parse_env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn parse_env_u32(name: &str, default: u32) -> u32 {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

#[derive(Debug)]
struct RollingSampleWindow {
    samples: Vec<f32>,
    write_index: usize,
    filled: usize,
}

impl RollingSampleWindow {
    fn new(sample_count: usize) -> Self {
        Self {
            samples: vec![0.0; sample_count.max(1)],
            write_index: 0,
            filled: 0,
        }
    }

    fn push(&mut self, sample: f32) {
        self.samples[self.write_index] = sample;
        self.write_index = (self.write_index + 1) % self.samples.len();
        self.filled = (self.filled + 1).min(self.samples.len());
    }

    fn snapshot(&self) -> Vec<f32> {
        let mut output = vec![0.0; self.samples.len()];
        if self.filled < self.samples.len() {
            let start = self.samples.len() - self.filled;
            output[start..].copy_from_slice(&self.samples[..self.filled]);
            return output;
        }

        let split = self.write_index;
        let tail = self.samples.len() - split;
        output[..tail].copy_from_slice(&self.samples[split..]);
        output[tail..].copy_from_slice(&self.samples[..split]);
        output
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[test]
    fn frame_encoding_uses_magic_count_and_little_endian_f32() {
        let frame = encode_frame(&[1.0, -0.5]);

        assert_eq!(&frame[..4], b"HZV1");
        assert_eq!(u32::from_le_bytes(frame[4..8].try_into().unwrap()), 2);
        assert_eq!(f32::from_le_bytes(frame[8..12].try_into().unwrap()), 1.0);
        assert_eq!(f32::from_le_bytes(frame[12..16].try_into().unwrap()), -0.5);
    }

    #[test]
    fn rolling_window_returns_latest_samples_with_zero_padding() {
        let mut window = RollingSampleWindow::new(4);

        window.push(1.0);
        window.push(2.0);
        assert_eq!(window.snapshot(), vec![0.0, 0.0, 1.0, 2.0]);

        window.push(3.0);
        window.push(4.0);
        window.push(5.0);
        assert_eq!(window.snapshot(), vec![2.0, 3.0, 4.0, 5.0]);
    }

    #[derive(Debug)]
    enum WriteAction {
        Write(usize),
        WouldBlock,
        Error(io::ErrorKind),
    }

    #[derive(Debug)]
    struct ScriptedWriter {
        actions: VecDeque<WriteAction>,
        written: Vec<u8>,
    }

    impl ScriptedWriter {
        fn new(actions: impl IntoIterator<Item = WriteAction>) -> Self {
            Self {
                actions: actions.into_iter().collect(),
                written: Vec::new(),
            }
        }
    }

    impl Write for ScriptedWriter {
        fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
            match self
                .actions
                .pop_front()
                .unwrap_or(WriteAction::Write(buf.len()))
            {
                WriteAction::Write(count) => {
                    let count = count.min(buf.len());
                    self.written.extend_from_slice(&buf[..count]);
                    Ok(count)
                }
                WriteAction::WouldBlock => Err(io::Error::new(io::ErrorKind::WouldBlock, "busy")),
                WriteAction::Error(kind) => Err(io::Error::new(kind, "failed")),
            }
        }

        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn pending_frame_retries_after_partial_write_and_would_block() {
        let bytes = vec![1, 2, 3, 4, 5, 6, 7, 8];
        let mut frame = PendingFrame::new(bytes.clone());
        let mut writer = ScriptedWriter::new([
            WriteAction::Write(3),
            WriteAction::WouldBlock,
            WriteAction::Write(5),
        ]);

        assert_eq!(
            flush_pending_frame(&mut writer, &mut frame),
            FrameSendResult::Pending
        );
        assert_eq!(frame.remaining(), &[4, 5, 6, 7, 8]);
        assert_eq!(writer.written, vec![1, 2, 3]);

        assert_eq!(
            flush_pending_frame(&mut writer, &mut frame),
            FrameSendResult::Complete
        );
        assert!(frame.is_complete());
        assert_eq!(writer.written, bytes);
    }

    #[test]
    fn pending_frame_disconnects_on_write_error() {
        let mut frame = PendingFrame::new(vec![1, 2, 3, 4]);
        let mut writer = ScriptedWriter::new([WriteAction::Error(io::ErrorKind::BrokenPipe)]);

        assert_eq!(
            flush_pending_frame(&mut writer, &mut frame),
            FrameSendResult::Disconnect
        );
        assert_eq!(frame.remaining(), &[1, 2, 3, 4]);
        assert!(writer.written.is_empty());
    }
}
