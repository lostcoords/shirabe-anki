use std::io::{self, BufRead};

use rand::{rngs::StdRng, Rng, SeedableRng};

fn main() {
    for line in io::stdin().lock().lines() {
        let line = line.unwrap();
        let mut parts = line.split_whitespace();
        let first = parts.next().unwrap();
        if first == "fuzz" {
            let lower = parts.next().unwrap().parse::<u32>().unwrap();
            let upper = parts.next().unwrap().parse::<u32>().unwrap();
            let bits = parts.next().unwrap().parse::<u32>().unwrap();
            let factor = f32::from_bits(bits);
            let interval = (lower as f32 + factor * ((1 + upper - lower) as f32)).floor() as u32;
            println!("{lower} {upper} {bits} {interval}");
            continue;
        }
        let seed = first.parse::<u64>().unwrap();
        if let Some(seconds) = parts.next() {
            let seconds = seconds.parse::<u32>().unwrap();
            let upper = seconds + ((seconds as f32 * 0.25).min(300.0).floor() as u32);
            let delay = if upper > seconds {
                StdRng::seed_from_u64(seed).random_range(seconds..upper)
            } else {
                seconds
            };
            println!("{seed} {seconds} {delay}");
            continue;
        }
        let factor: f32 = StdRng::seed_from_u64(seed).random_range(0.0..1.0);
        println!("{seed} {}", factor.to_bits());
    }
}
