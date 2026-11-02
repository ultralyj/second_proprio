//! 执行短生命周期的本机管理命令，同时排空输出并限制总等待时间。

use std::{
    io::Read,
    process::{Command, Output, Stdio},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};

const OUTPUT_LIMIT: usize = 8 * 1024 * 1024;
const TIMEOUT_MESSAGE: &str = "操作超时，请查看后台状态";

fn drain_output(mut pipe: impl Read + Send + 'static) -> Receiver<Result<Vec<u8>, String>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let result = (|| {
            let mut output = Vec::new();
            let mut buffer = [0; 8192];
            let mut exceeded = false;
            loop {
                let size = pipe.read(&mut buffer).map_err(|error| error.to_string())?;
                if size == 0 {
                    break;
                }
                if output.len() + size <= OUTPUT_LIMIT && !exceeded {
                    output.extend_from_slice(&buffer[..size]);
                } else {
                    // 超限后继续排空，避免子进程堵在写入端；不再积累内存。
                    exceeded = true;
                }
            }
            if exceeded {
                Err("命令输出超过 8 MiB，请查看后台日志".into())
            } else {
                Ok(output)
            }
        })();
        let _ = sender.send(result);
    });
    receiver
}

fn receive_output(
    receiver: Receiver<Result<Vec<u8>, String>>,
    deadline: Instant,
) -> Result<Vec<u8>, String> {
    receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .map_err(|_| TIMEOUT_MESSAGE.to_string())?
}

/// 并发读取标准输出与错误；超时终止命令，不把退出前的管道阻塞误报为超时。
pub(crate) fn run_bounded(mut command: Command, timeout: Duration) -> Result<Output, String> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().map_err(|error| error.to_string())?;
    let deadline = Instant::now() + timeout;
    let stdout = drain_output(child.stdout.take().ok_or("无法读取命令输出")?);
    let stderr = drain_output(child.stderr.take().ok_or("无法读取命令错误")?);
    loop {
        if let Some(status) = child.try_wait().map_err(|error| error.to_string())? {
            return Ok(Output {
                status,
                stdout: receive_output(stdout, deadline)?,
                stderr: receive_output(stderr, deadline)?,
            });
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(TIMEOUT_MESSAGE.into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn drains_output_larger_than_pipe_capacity() {
        let mut command = Command::new("/bin/sh");
        command.args([
            "-c",
            "head -c 262144 /dev/zero; head -c 262144 /dev/zero >&2",
        ]);
        let output = run_bounded(command, Duration::from_secs(5)).unwrap();
        assert!(output.status.success());
        assert_eq!(output.stdout.len(), 262144);
        assert_eq!(output.stderr.len(), 262144);
    }

    #[test]
    fn times_out_and_retains_nonzero_exit_status() {
        let mut slow = Command::new("/bin/sleep");
        slow.arg("5");
        assert_eq!(
            run_bounded(slow, Duration::from_millis(50)).unwrap_err(),
            TIMEOUT_MESSAGE
        );
        let mut failed = Command::new("/bin/sh");
        failed.args(["-c", "printf '失败' >&2; exit 7"]);
        let output = run_bounded(failed, Duration::from_secs(2)).unwrap();
        assert_eq!(output.status.code(), Some(7));
        assert_eq!(String::from_utf8(output.stderr).unwrap(), "失败");
    }
}
