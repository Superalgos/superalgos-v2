# Pinned trust bundle (version 2026-10-12)

Vendored from prototype 5b. `tsas.json` is `{"bundle_version", "entries"}`; a new download is a new
version. Old proofs stay verifiable under a newer bundle because chains are checked at genTime.
The download script lives in the prototype (`tools/build_trust.py`); it is not part of sa-core.

`tsas.json` lists the pinned roots; `roots/*.pem` are the root certificates (one each). The verifier
refuses to load a bundle where a root's SHA-256 (of the DER) differs from `tsas.json`.

Every root was downloaded on 2026-10-12 by `tools/build_trust.py` from the URL in the table: the
`caIssuers` (AIA) URL inside the TSA's own certificate chain, or the vendor's published file
(FreeTSA also publishes the SHA-256 of `cacert.pem`, `2151b611...`, which matches the file).
Two further independent checks, recorded per root: whether the same certificate is in Mozilla's
store (via `certifi`) and in macOS's system roots. Roots in neither (FreeTSA, SwissSign, DFN,
Sectigo qualified) rest on the vendor download alone.

`operator` is the organisation the k-of-n rule counts. Two URLs can share an operator: Entrust's URL
now answers with a Sectigo signer; QuoVadis is DigiCert. `allow_sha1_signature` is on for Apple
only: its CMS signature uses SHA-1 (the stamped imprint is still SHA-256).

| entry | operator | root SHA-256 (DER) | downloaded from | Mozilla | macOS | accepted policy OIDs |
|---|---|---|---|---|---|---|
| freetsa | freetsa | `a6379e7cecc05faa3cbf076013d745e327bbbaa38c0b9af22469d4701d18aabc` | https://freetsa.org/files/cacert.pem | no | no | 1.2.3.4.1 |
| digicert | digicert | `552f7bdcf1a7af9e6ce672017f4f12abf77240c78e761ac203d1d9d20ac89988` | http://cacerts.digicert.com/DigiCertTrustedRootG4.crt | yes | yes | 2.16.840.1.114412.7.1 |
| digicert-quovadis | digicert | `8a866fd1b276b57e578e921c65828a2bed58e9f2f288054134b7f1f4bfc9cc74` | http://trust.quovadisglobal.com/qvrca1g3.crt | yes | yes | 1.3.6.1.4.1.8024.0.2000.6 |
| sectigo | sectigo | `4941b001b8a97e961b7817c9d9e960ec4b056bfc915a8c1aabf6ef6b3ac046a5` | http://crt.sectigo.com/SectigoPublicTimeStampingRootR46.p7c | no | yes | 1.3.6.1.4.1.6449.2.1.1 |
| sectigo-qualified | sectigo | `f871f8976b4068d700d5f281084b4a29eaf4b8f35743330ba062fab46f58c2ed` | http://crt.sectigo.com/SectigoQualifiedTimeStampingRootR45.crt | no | no | 0.4.0.2023.1.1 |
| globalsign | globalsign | `2cabeafe37d06ca22aba7391c0033d25982952c453647349763a3ab5ad6ccf69` | http://secure.globalsign.com/cacert/root-r6.crt | yes | yes | 1.3.6.1.4.1.4146.2.3.2.2 |
| apple | apple | `b0b1730ecbc7ff4505142c49f1295e6eda6bcaed7e2c68c5be91b5a11001f024` | https://www.apple.com/appleca/AppleIncRootCertificate.cer | no | yes | 1.2.3 |
| swisssign | swisssign | `b87f292a4d9feace2d669159eb26f56d85ec77c19e01098cd754e8abb310cde5` | http://swisssign.net/cgi-bin/authority/download/C99B6176377D397796F4A1E97BCDEB2070F2E084 | no | no | 2.16.756.1.89.1.1.3.5 |
| certum | certum | `5c58468d55f58e497e743982d2b50010b6d165374acf83a7d4a32db768c4408e` | http://repository.certum.pl/ctnca.cer | yes | yes | 1.2.616.1.113527.2.5.1.11 |
| identrust | identrust | `30d0895a9a448a262091635522d1f52010b5867acae12c78ef958fd4f4389f2f` | http://validation.identrust.com/roots/publicrootca1.p7c | yes | yes | 2.16.840.1.113839.0.6.13.3 |
| sslcom | sslcom | `85666a562ee0be5ce925c1d8890a6f76a87ec16d4d7d5f29ea7419cf20123b69` | http://www.ssl.com/repository/SSLcomRootCertificationAuthorityRSA.crt | yes | yes | 1.3.6.1.4.1.38064.1.3.6.1 |
| dfn | dfn | `3cdc2c9e9e5a36cb5888fd1796cb912f846253b682c1b32057532033510c7bb6` | http://cdp1.pca.dfn.de/dfn-verein-community-root-ca/pub/cacert/cacert.crt | no | no | 1.3.6.1.4.1.22177.300.22.1 |

Not pinned: MeSince (`tsa.mesign.com`): answers anonymously, but its root is self-issued by an
unknown party and appears in no store; Actalis answered HTTP 500 to the probe.
