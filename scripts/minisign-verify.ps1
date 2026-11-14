# Minisign verification for the native Windows installer.
#
# The installer is signed with Tauri's minisign key (Ed25519). This file
# embeds a small, self-contained verifier (BLAKE2b-512 + Ed25519) so the
# one-line PowerShell installer can check the signature without Python,
# minisign, or any other runtime.
#
# The public key is the same value pinned in spend_app/native_update.py and
# plugins.updater.pubkey in tauri.conf.json.

$ErrorActionPreference = 'Stop'

$MinisignPublicKey = 'dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDVDQUNGNEMxNUMxRkNENkQKUldSdHpSOWN3ZlNzWE9yblR0Q1BIZUFTY3VhbWFFeHB2OWVkOVZhUjF5dVNaQm1CWHZTM0I3WkMK'

if (-not ('Burnrate.Minisign' -as [type])) {
    Add-Type -ReferencedAssemblies 'System.Numerics' -TypeDefinition @'
using System;
using System.IO;
using System.Linq;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;

namespace Burnrate
{
    public sealed class Blake2b
    {
        private static readonly ulong[] IV = new ulong[]
        {
            0x6a09e667f3bcc908UL, 0xbb67ae8584caa73bUL,
            0x3c6ef372fe94f82bUL, 0xa54ff53a5f1d36f1UL,
            0x510e527fade682d1UL, 0x9b05688c2b3e6c1fUL,
            0x1f83d9abfb41bd6bUL, 0x5be0cd19137e2179UL
        };

        private static readonly byte[,] Sigma = new byte[,]
        {
            { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15 },
            { 14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3 },
            { 11, 8, 12, 0, 5, 2, 15, 13, 10, 14, 3, 6, 7, 1, 9, 4 },
            { 7, 9, 3, 1, 13, 12, 11, 14, 2, 6, 5, 10, 4, 0, 15, 8 },
            { 9, 0, 5, 7, 2, 4, 10, 15, 14, 1, 11, 12, 6, 8, 3, 13 },
            { 2, 12, 6, 10, 0, 11, 8, 3, 4, 13, 7, 5, 15, 14, 1, 9 },
            { 12, 5, 1, 15, 14, 13, 4, 10, 0, 7, 6, 3, 9, 2, 8, 11 },
            { 13, 11, 7, 14, 12, 1, 3, 9, 5, 0, 15, 4, 8, 6, 2, 10 },
            { 6, 15, 14, 9, 11, 3, 0, 8, 12, 2, 13, 7, 1, 4, 10, 5 },
            { 10, 2, 8, 4, 7, 6, 1, 5, 15, 11, 9, 14, 3, 12, 13, 0 },
            { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15 },
            { 14, 10, 4, 8, 9, 15, 13, 6, 1, 12, 0, 2, 11, 7, 5, 3 }
        };

        private readonly ulong[] h = new ulong[8];
        private readonly byte[] buffer = new byte[128];
        private readonly int outputLength;
        private ulong counterLow;
        private ulong counterHigh;
        private int bufferLength;
        private bool finished;

        public Blake2b(int outputLength)
        {
            if (outputLength < 1 || outputLength > 64) throw new ArgumentOutOfRangeException("outputLength");
            this.outputLength = outputLength;
            Array.Copy(IV, h, 8);
            h[0] ^= 0x01010000UL ^ (ulong)outputLength;
        }

        public static byte[] Hash(byte[] data, int outputLength)
        {
            Blake2b hasher = new Blake2b(outputLength);
            hasher.Update(data, 0, data.Length);
            return hasher.Finish();
        }

        public void Update(byte[] data, int offset, int count)
        {
            if (finished) throw new InvalidOperationException("finished");
            if (data == null) throw new ArgumentNullException("data");
            if (offset < 0 || count < 0 || offset + count > data.Length) throw new ArgumentOutOfRangeException();
            while (count > 0)
            {
                if (bufferLength == 128)
                {
                    counterLow += 128;
                    if (counterLow < 128) counterHigh++;
                    Compress(false);
                    bufferLength = 0;
                }
                int take = Math.Min(count, 128 - bufferLength);
                Array.Copy(data, offset, buffer, bufferLength, take);
                bufferLength += take;
                offset += take;
                count -= take;
            }
        }

        public byte[] Finish()
        {
            if (finished) throw new InvalidOperationException("finished");
            finished = true;
            counterLow += (ulong)bufferLength;
            if (counterLow < (ulong)bufferLength) counterHigh++;
            for (int i = bufferLength; i < 128; i++) buffer[i] = 0;
            Compress(true);
            byte[] result = new byte[64];
            for (int i = 0; i < 8; i++)
            {
                byte[] word = BitConverter.GetBytes(h[i]);
                Array.Copy(word, 0, result, i * 8, 8);
            }
            if (outputLength == 64) return result;
            byte[] shortened = new byte[outputLength];
            Array.Copy(result, shortened, outputLength);
            return shortened;
        }

        private static ulong RotateRight(ulong value, int bits)
        {
            return (value >> bits) | (value << (64 - bits));
        }

        private static void G(ulong[] v, int a, int b, int c, int d, ulong x, ulong y)
        {
            v[a] = v[a] + v[b] + x;
            v[d] = RotateRight(v[d] ^ v[a], 32);
            v[c] = v[c] + v[d];
            v[b] = RotateRight(v[b] ^ v[c], 24);
            v[a] = v[a] + v[b] + y;
            v[d] = RotateRight(v[d] ^ v[a], 16);
            v[c] = v[c] + v[d];
            v[b] = RotateRight(v[b] ^ v[c], 63);
        }

        private void Compress(bool last)
        {
            ulong[] m = new ulong[16];
            for (int i = 0; i < 16; i++) m[i] = BitConverter.ToUInt64(buffer, i * 8);

            ulong[] v = new ulong[16];
            Array.Copy(h, 0, v, 0, 8);
            Array.Copy(IV, 0, v, 8, 8);
            v[12] ^= counterLow;
            v[13] ^= counterHigh;
            if (last) v[14] = ~v[14];

            for (int round = 0; round < 12; round++)
            {
                G(v, 0, 4, 8, 12, m[Sigma[round, 0]], m[Sigma[round, 1]]);
                G(v, 1, 5, 9, 13, m[Sigma[round, 2]], m[Sigma[round, 3]]);
                G(v, 2, 6, 10, 14, m[Sigma[round, 4]], m[Sigma[round, 5]]);
                G(v, 3, 7, 11, 15, m[Sigma[round, 6]], m[Sigma[round, 7]]);
                G(v, 0, 5, 10, 15, m[Sigma[round, 8]], m[Sigma[round, 9]]);
                G(v, 1, 6, 11, 12, m[Sigma[round, 10]], m[Sigma[round, 11]]);
                G(v, 2, 7, 8, 13, m[Sigma[round, 12]], m[Sigma[round, 13]]);
                G(v, 3, 4, 9, 14, m[Sigma[round, 14]], m[Sigma[round, 15]]);
            }
            for (int i = 0; i < 8; i++) h[i] ^= v[i] ^ v[i + 8];
        }
    }

    public sealed class Ed25519
    {
        private static readonly BigInteger P = BigInteger.Pow(2, 255) - 19;
        private static readonly BigInteger L = BigInteger.Pow(2, 252) + BigInteger.Parse("27742317777372353535851937790883648493");
        private static readonly BigInteger D = Mod(-121665 * Inv(121666));
        private static readonly BigInteger SqrtM1 = BigInteger.ModPow(2, (P - 1) / 4, P);
        private static readonly Point Base = MakeBase();

        private sealed class Point
        {
            public BigInteger X;
            public BigInteger Y;
            public BigInteger Z;
            public BigInteger T;
            public Point(BigInteger x, BigInteger y, BigInteger z, BigInteger t)
            {
                X = x; Y = y; Z = z; T = t;
            }
        }

        private static BigInteger Mod(BigInteger value)
        {
            BigInteger result = value % P;
            return result.Sign < 0 ? result + P : result;
        }

        private static BigInteger Inv(BigInteger value)
        {
            return BigInteger.ModPow(Mod(value), P - 2, P);
        }

        private static BigInteger? RecoverX(BigInteger y, int sign)
        {
            if (y >= P) return null;
            BigInteger y2 = Mod(y * y);
            BigInteger x2 = Mod((y2 - 1) * Inv(Mod(D * y2 + 1)));
            if (x2.IsZero) return sign == 0 ? (BigInteger?)BigInteger.Zero : null;
            BigInteger x = BigInteger.ModPow(x2, (P + 3) / 8, P);
            if (Mod(x * x - x2) != 0) x = Mod(x * SqrtM1);
            if (Mod(x * x - x2) != 0) return null;
            int parity = x.IsEven ? 0 : 1;
            if (parity != sign) x = P - x;
            return x;
        }

        private static BigInteger LittleEndian(byte[] data, int offset, int length)
        {
            byte[] copy = new byte[length + 1];
            Array.Copy(data, offset, copy, 0, length);
            return new BigInteger(copy);
        }

        private static Point Decode(byte[] data, int offset)
        {
            if (data.Length - offset < 32) return null;
            byte[] copy = new byte[32];
            Array.Copy(data, offset, copy, 0, 32);
            int sign = (copy[31] & 0x80) != 0 ? 1 : 0;
            copy[31] = (byte)(copy[31] & 0x7f);
            BigInteger y = new BigInteger(copy.Concat(new byte[] { 0 }).ToArray());
            BigInteger? x = RecoverX(y, sign);
            if (!x.HasValue) return null;
            return new Point(x.Value, y, BigInteger.One, Mod(x.Value * y));
        }

        private static Point Add(Point p, Point q)
        {
            BigInteger a = Mod((p.Y - p.X) * (q.Y - q.X));
            BigInteger b = Mod((p.Y + p.X) * (q.Y + q.X));
            BigInteger c = Mod(2 * p.T * q.T * D);
            BigInteger d = Mod(2 * p.Z * q.Z);
            BigInteger e = b - a;
            BigInteger f = d - c;
            BigInteger g = d + c;
            BigInteger h = b + a;
            return new Point(Mod(e * f), Mod(g * h), Mod(f * g), Mod(e * h));
        }

        private static Point Multiply(BigInteger scalar, Point point)
        {
            Point result = new Point(BigInteger.Zero, BigInteger.One, BigInteger.One, BigInteger.Zero);
            Point addend = point;
            while (scalar.Sign > 0)
            {
                if (!scalar.IsEven) result = Add(result, addend);
                addend = Add(addend, addend);
                scalar = scalar >> 1;
            }
            return result;
        }

        private static bool Same(Point p, Point q)
        {
            return Mod(p.X * q.Z - q.X * p.Z).IsZero && Mod(p.Y * q.Z - q.Y * p.Z).IsZero;
        }

        private static Point MakeBase()
        {
            BigInteger y = Mod(4 * Inv(5));
            BigInteger? x = RecoverX(y, 0);
            if (!x.HasValue) throw new InvalidOperationException("base point");
            return new Point(x.Value, y, BigInteger.One, Mod(x.Value * y));
        }

        public static bool Verify(byte[] publicKey, byte[] message, byte[] signature)
        {
            if (publicKey == null || publicKey.Length != 32) return false;
            if (signature == null || signature.Length != 64) return false;
            Point a = Decode(publicKey, 0);
            Point r = Decode(signature, 0);
            if (a == null || r == null) return false;
            BigInteger s = LittleEndian(signature, 32, 32);
            if (s >= L) return false;
            byte[] hashInput = new byte[32 + 32 + message.Length];
            Array.Copy(signature, 0, hashInput, 0, 32);
            Array.Copy(publicKey, 0, hashInput, 32, 32);
            Array.Copy(message, 0, hashInput, 64, message.Length);
            byte[] digest;
            using (SHA512 sha = SHA512.Create()) digest = sha.ComputeHash(hashInput);
            BigInteger k = LittleEndian(digest, 0, 64) % L;
            return Same(Multiply(s, Base), Add(r, Multiply(k, a)));
        }
    }

    public static class Minisign
    {
        private static bool Equal(byte[] a, byte[] b)
        {
            if (a.Length != b.Length) return false;
            for (int i = 0; i < a.Length; i++) if (a[i] != b[i]) return false;
            return true;
        }

        private static string[] Lines(string text)
        {
            return text.Replace("\r", "").Split(new char[] { '\n' }, StringSplitOptions.RemoveEmptyEntries);
        }

        public static bool VerifyFile(string path, string signatureBase64, string publicKeyBase64, string expectedFile)
        {
            try
            {
                string[] keyLines = Lines(Encoding.UTF8.GetString(Convert.FromBase64String(publicKeyBase64.Trim())));
                string[] sigLines = Lines(Encoding.UTF8.GetString(Convert.FromBase64String(signatureBase64.Trim())));
                if (keyLines.Length < 2 || sigLines.Length < 4) return false;

                byte[] keyBlob = Convert.FromBase64String(keyLines[1].Trim());
                byte[] sigBlob = Convert.FromBase64String(sigLines[1].Trim());
                byte[] global = Convert.FromBase64String(sigLines[3].Trim());
                if (keyBlob.Length < 42 || sigBlob.Length < 74 || global.Length != 64) return false;

                byte[] keyId = new byte[8];
                byte[] sigId = new byte[8];
                Array.Copy(keyBlob, 2, keyId, 0, 8);
                Array.Copy(sigBlob, 2, sigId, 0, 8);
                if (!Equal(keyId, sigId)) return false;

                byte[] publicKey = new byte[32];
                byte[] signature = new byte[64];
                Array.Copy(keyBlob, 10, publicKey, 0, 32);
                Array.Copy(sigBlob, 10, signature, 0, 64);

                string algorithm = Encoding.ASCII.GetString(sigBlob, 0, 2);
                byte[] data = File.ReadAllBytes(path);
                byte[] message = algorithm == "ED" ? Blake2b.Hash(data, 64) : data;
                if (!Ed25519.Verify(publicKey, message, signature)) return false;

                string trustedLine = sigLines[2].Trim();
                string fields = trustedLine.StartsWith("trusted comment: ", StringComparison.Ordinal)
                    ? trustedLine.Substring("trusted comment: ".Length)
                    : trustedLine;
                byte[] trustedBytes = Encoding.UTF8.GetBytes(fields);
                byte[] signedComment = new byte[signature.Length + trustedBytes.Length];
                Array.Copy(signature, 0, signedComment, 0, signature.Length);
                Array.Copy(trustedBytes, 0, signedComment, signature.Length, trustedBytes.Length);
                if (!Ed25519.Verify(publicKey, signedComment, global)) return false;

                bool fileMatches = false;
                foreach (string part in fields.Split('\t'))
                {
                    if (part.StartsWith("file:", StringComparison.Ordinal))
                    {
                        fileMatches = part.Substring(5) == expectedFile;
                        break;
                    }
                }
                return fileMatches;
            }
            catch
            {
                return false;
            }
        }
    }
}
'@
}

function Test-MinisignFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$SignaturePath,
        [Parameter(Mandatory = $true)][string]$ExpectedFile
    )
    if (-not (Test-Path -LiteralPath $Path)) { throw "Signed file not found: $Path" }
    if (-not (Test-Path -LiteralPath $SignaturePath)) { throw "Signature not found: $SignaturePath" }
    $signature = Get-Content -LiteralPath $SignaturePath -Raw
    if (-not [Burnrate.Minisign]::VerifyFile($Path, $signature, $MinisignPublicKey, $ExpectedFile)) {
        throw "The installer's minisign signature is missing, invalid, or names a different file."
    }
}
