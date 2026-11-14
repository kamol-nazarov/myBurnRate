# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

> **Status:** the application to SignPath Foundation is pending. Until it is
> approved, Windows installers are not code-signed and Windows SmartScreen
> warns before running them. Every release file is still built on GitHub
> Actions with a published build provenance record, and each one is listed in
> the release's `SHA256SUMS`.

## What is signed

The Windows installers, `BURNRATE_<version>_x64-setup.exe` (the Tauri app) and
`myBurnRate_<version>_x64-setup.exe` (the native app), are
code-signed, and only when built by this repository's
[release workflow](../.github/workflows/release.yml) from a version tag. Local
builds and the per-commit builds in the Desktop and Native desktop workflows are
never signed by the project.

A certificate has to be supplied by the project owner: until the SignPath
Foundation application is approved (or an Azure Trusted Signing account is set
up, the paid alternative), neither installer carries an Authenticode signature
and Windows SmartScreen or Defender may warn about it. In the native release
job SignPath signs the installer only; `BURNRATE.exe` inside it is signed only
when a build is run with the `BURNRATE_SIGN_*` variables below.

Separately from Authenticode, every installer also has an **update signature**
(a minisign signature made with the Tauri updater key). The native app checks
it, with the installer's size and SHA-256, before it installs an update, and
the release workflow verifies it against the key built into the app.

Each signing request is approved by hand in SignPath before the certificate is
used. The installer is signed after it is built, and its in-app update
signature is made again for the signed file.

macOS and Linux bundles are not code-signed. The macOS app is ad-hoc signed
only; see the README for opening it the first time.

## Team roles

| Role | Members |
| --- | --- |
| Committers and reviewers | [kamol-nazarov](https://github.com/kamol-nazarov) |
| Approvers (approve each signing request) | [kamol-nazarov](https://github.com/kamol-nazarov) |

Changes from anyone else arrive as pull requests, and a committer reviews them
before they are merged. Everyone with these roles uses multi-factor
authentication for GitHub and SignPath.

## Privacy

This program will not transfer any information to other networked systems
unless the person using or installing it asks for it. The exceptions are:

- **Update checks.** An installed Tauri app asks GitHub Releases for
  `latest.json` about every six hours to see whether a newer version exists.
  This sends no usage data. To turn it off, set `BURNRATE_UPDATES=0`.
  An installed native app asks for `native-latest.json` once after it starts
  and then daily; to turn that off, set `BURNRATE_NO_UPDATE_CHECK=1`. See the
  [privacy notes](privacy.md#update-checks-native-app).
- **Connections the person sets up.** These are a hub they run themselves,
  provider admin or balance keys they paste in, and the sign-in-reuse switches
  in setup. Each one contacts only its own allowlisted host.

The full data flow is in the [privacy notes](privacy.md).

## Setting up signing (maintainers)

1. Apply at <https://signpath.org/apply> for the `kamol-nazarov/myBurnRate`
   repository.
2. In SignPath, create the project with slug `myBurnRate`. Link it to the
   predefined **GitHub.com** trusted build system, and install the SignPath
   GitHub App on the repository.
3. Give the project this artifact configuration. The release workflow uploads
   the installer zipped, the GitHub default:

   ```xml
   <?xml version="1.0" encoding="utf-8"?>
   <artifact-configuration xmlns="http://signpath.io/artifact-configuration/v1">
     <zip-file>
       <pe-file path="*-setup.exe">
         <authenticode-sign/>
       </pe-file>
     </zip-file>
   </artifact-configuration>
   ```

4. Create a signing policy, for example `release-signing`, that requires
   manual approval. Add a CI user with submitter rights and an API token.
5. In the GitHub repository settings:
   - add the secret `SIGNPATH_API_TOKEN`;
   - add the variables `SIGNPATH_ORGANIZATION_ID`, `SIGNPATH_PROJECT_SLUG`
     (`myBurnRate`) and `SIGNPATH_SIGNING_POLICY_SLUG`.

   The release workflow's signing job runs once `SIGNPATH_ORGANIZATION_ID` is
   set. Until then it is skipped and the installer ships unsigned.

## Signing a local native build

`packaging/build_native.py` signs `BURNRATE.exe` before it is packaged and the installer after it
is built, using `signtool`, when exactly one of these methods is configured through environment
variables. Signatures use SHA-256 and an RFC 3161 timestamp, and each signed file is verified
afterwards. With nothing configured the build is unsigned and prints one line saying so;
`--require-signature` makes it fail instead.

| Method | Variables |
| --- | --- |
| Certificate in the Windows store | `BURNRATE_SIGN_THUMBPRINT` (40 hex; `BURNRATE_SIGN_MACHINE_STORE=1` for the machine store) |
| PFX file | `BURNRATE_SIGN_PFX` and `BURNRATE_SIGN_PFX_PASSWORD` |
| Azure Trusted Signing | `BURNRATE_SIGN_AZURE_DLIB` (`Azure.CodeSigning.Dlib.dll`), `BURNRATE_SIGN_AZURE_METADATA` (metadata JSON), plus the usual `AZURE_*` identity variables |

Optional: `BURNRATE_SIGNTOOL` (path to `signtool.exe`; otherwise the PATH and the Windows SDK are
searched) and `BURNRATE_SIGN_TIMESTAMP_URL`. The PFX password is read from the environment only
and never put on a command line or printed: the PFX is imported through PowerShell, used by
thumbprint and removed again.

```powershell
$env:BURNRATE_SIGN_THUMBPRINT = "<thumbprint>"
python packaging/build_native.py --require-installer --require-signature
```

## Known limits

- **Only the installer is signed in the SignPath path.** The `BURNRATE.exe` inside the native
  installer is not Authenticode-signed there, unless the SignPath artifact configuration is
  extended to sign the nested executable. A local build with a certificate (or Azure Trusted
  Signing) signs both `BURNRATE.exe` and the installer.
- **`uninstall.exe` is never signed.** NSIS generates it at install time, on the user's computer,
  so no signing step can reach it.

## What a signed app accepts as an update

If the running `BURNRATE.exe` is Authenticode-signed, the native updater only installs an update
whose installer is also validly signed by the same publisher, so an update never turns a signed
app into an unsigned one. That match is a secondary control: SignPath Foundation certificates share
one subject across the projects they sign, so a matching subject does not prove an installer is
BURNRATE's. The primary control is the minisign check against the update key pinned in the app,
which also binds the signature to the exact installer file name.
