#[cfg(windows)]
mod capture;
mod pixels;
mod protocol;

#[cfg(not(windows))]
use protocol::Failure;
use protocol::{Request, Response};
use std::io::{self, Write};

fn main() {
    if let Err(error) = run() {
        eprintln!("capture worker I/O failed: {error}");
        std::process::exit(1);
    }
}

fn run() -> io::Result<()> {
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let stdout = io::stdout();
    let mut output = stdout.lock();
    #[cfg(windows)]
    let mut worker = capture::Worker::default();
    loop {
        let line = match protocol::read_line(&mut input)? {
            None => break,
            Some(line) => line,
        };
        let id = serde_json::from_slice::<serde_json::Value>(&line)
            .ok()
            .and_then(|value| value.get("id").and_then(|id| id.as_u64()))
            .unwrap_or(0);
        let request = Request::parse(&line);
        #[cfg(windows)]
        let result = request.and_then(|request| worker.execute(request));
        #[cfg(not(windows))]
        let result = request.and_then(|_| {
            Err(Failure::new(
                "unsupported_platform",
                "Windows 10 build 19041 or later is required",
            ))
        });
        let response = match result {
            Ok((metadata, pixels)) => Response::success(id, metadata, pixels),
            Err(error) => Response::failure(id, error),
        };
        serde_json::to_writer(&mut output, &response.header)?;
        output.write_all(b"\n")?;
        output.write_all(&response.pixels)?;
        output.flush()?;
        #[cfg(windows)]
        if worker.must_exit {
            break;
        }
    }
    #[cfg(windows)]
    worker.close();
    Ok(())
}
