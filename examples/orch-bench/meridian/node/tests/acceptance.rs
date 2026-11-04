// Hidden acceptance: R4 (CRC-32 and the record log).
use std::fs;
use std::io::Write;
use std::path::PathBuf;

use mkv_node::{crc32, log};

fn scratch(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("mkv-acceptance-{}-{}", name, std::process::id()));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir.join("log")
}

fn record_len(payload: &str) -> u64 {
    8 + payload.len() as u64
}

#[test]
fn acceptance_crc32_vectors() {
    assert_eq!(crc32(b"123456789"), 0xCBF4_3926);
    assert_eq!(crc32(b""), 0);
    assert_eq!(crc32(b"a"), 0xE8B7_BE43);
}

#[test]
fn acceptance_record_layout() {
    let path = scratch("layout");
    log::append(&path, "PUTV a 1 w x").unwrap();
    let bytes = fs::read(&path).unwrap();
    assert_eq!(&bytes[0..4], &[0, 0, 0, 12]);
    assert_eq!(&bytes[4..16], b"PUTV a 1 w x");
    assert_eq!(&bytes[16..20], &0x8A7C_4E8Bu32.to_be_bytes());
    assert_eq!(bytes.len(), 20);
}

#[test]
fn acceptance_absent_log_and_round_trip() {
    let path = scratch("roundtrip");
    assert_eq!(log::replay(&path).unwrap(), Vec::<String>::new());
    let payloads = ["PUTV k%C3%A9 1 n1 v%20w", "DELV k 2 n1", "PUTV x 3 r1 ="];
    for payload in payloads {
        log::append(&path, payload).unwrap();
    }
    assert_eq!(log::replay(&path).unwrap(), payloads.to_vec());
    let expected: u64 = payloads.iter().map(|p| record_len(p)).sum();
    assert_eq!(fs::metadata(&path).unwrap().len(), expected);
}

#[test]
fn acceptance_torn_tail_is_truncated_and_log_continues() {
    let path = scratch("torn");
    log::append(&path, "PUTV a 1 n1 x").unwrap();
    log::append(&path, "PUTV b 1 n1 y").unwrap();
    let good = fs::metadata(&path).unwrap().len();
    {
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(&[0, 0, 0, 100]).unwrap(); // a header promising 100 bytes
        file.write_all(b"PUTV c").unwrap(); // ... of which only a few were written
    }
    assert_eq!(log::replay(&path).unwrap(), vec!["PUTV a 1 n1 x", "PUTV b 1 n1 y"]);
    assert_eq!(fs::metadata(&path).unwrap().len(), good);
    log::append(&path, "PUTV c 1 n1 z").unwrap();
    assert_eq!(log::replay(&path).unwrap().len(), 3);
}

#[test]
fn acceptance_bad_crc_stops_replay() {
    let path = scratch("crc");
    for payload in ["PUTV a 1 n1 x", "PUTV b 1 n1 y", "PUTV c 1 n1 z"] {
        log::append(&path, payload).unwrap();
    }
    let mut bytes = fs::read(&path).unwrap();
    let second_payload = record_len("PUTV a 1 n1 x") as usize + 4;
    bytes[second_payload] ^= 0x20; // flip a bit inside the second payload
    fs::write(&path, &bytes).unwrap();
    assert_eq!(log::replay(&path).unwrap(), vec!["PUTV a 1 n1 x"]);
    assert_eq!(fs::metadata(&path).unwrap().len(), record_len("PUTV a 1 n1 x"));
}

#[test]
fn acceptance_short_header_is_a_torn_tail() {
    let path = scratch("header");
    log::append(&path, "PUTV a 1 n1 x").unwrap();
    {
        let mut file = fs::OpenOptions::new().append(true).open(&path).unwrap();
        file.write_all(&[0, 0]).unwrap();
    }
    assert_eq!(log::replay(&path).unwrap(), vec!["PUTV a 1 n1 x"]);
    assert_eq!(fs::metadata(&path).unwrap().len(), record_len("PUTV a 1 n1 x"));
}
