# Code signing policy

Free code signing provided by [SignPath.io](https://about.signpath.io/), certificate by [SignPath Foundation](https://signpath.org/).

> **Status:** the application to SignPath Foundation is pending. Until it is
> approved, Windows installers are not code-signed and Windows SmartScreen
> warns before running them. Every release file is still built on GitHub
> Actions with a published build provenance record, and each one is listed in
> the release's `SHA256SUMS`.

## What is signed

Only the Windows installer (`BURNRATE_<version>_x64-setup.exe`) is
code-signed, and only when it is built by this repository's
[release workflow](../.github/workflows/release.yml) from a version tag. Local
builds and the per-commit builds in the Desktop workflow are never signed.

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

- **Update checks.** An installed app asks GitHub Releases for
  `latest.json` about every six hours to see whether a newer version exists.
  This sends no usage data. To turn it off, set `BURNRATE_UPDATES=0`.
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
