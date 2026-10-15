# Compliance boundaries

Last updated: 15 October 2026

**Varyz implements privacy controls; passing tests is not certification of compliance with every law or provider contract.** Local software, optional Cloud storage, public sharing and hosted plugins have different boundaries. The operator remains responsible for the actual deployment and legal obligations. This document describes code and operating requirements without asserting that third-party agreements have been signed.

| Guardrail | Enforcement | Legal or platform basis |
|---|---|---|
| Approved sources and projects | Local source approval; Cloud project selection, pause/revoke and tenant-scoped store queries | GDPR purpose limitation, minimization and data protection by default |
| Sensitive/unclassified history excluded | Archive Document validator permits only public/personal labels; client-side privacy preflight restricts upload | Privacy policy; GDPR Articles 5 and 25; a label is not proof that content is safe |
| Detected secrets and payment-card numbers rejected | `privacy/secrets_scan.py`, Cloud preference/archive/report validators | Hosted platform restricted-data rules and credential security |
| Hosted prohibited-data heuristics | Cloud-specific input and output screening, including old stored records on retrieval; privacy export/deletion stay available | OpenAI restricted-data/child-information rules; heuristics cannot guarantee recognition |
| Notice before account data collection | Cloud auth page rendering places canonical privacy access before sign-in/signup/social-completion/recovery controls | GDPR transparency; OpenAI developer terms section 2.3 |
| Independent permission boundaries | OAuth scopes/PKCE; hosted plugin cannot approve projects, upload history, change billing or delete account | Least privilege and explicit user intent |
| Export, revocation and erasure | Authenticated account/project controls; privacy actions remain available without a paid subscription | Applicable GDPR access, portability and erasure rights, with lawful exceptions |
| Limited disclosed retention | Account/revision lifecycle, application backups, security-log configuration and analytics expiry | GDPR storage limitation; timing/outages and independent recipient records are disclosed |
| Billing outside hosted plugin | Paddle checkout/account controls; plugin management actions denied | Current OpenAI digital-commerce restrictions; Paddle supplier contract applies separately |
| Synthetic verification | Cloud tenant, privacy, OAuth, sharing, signup and restricted-data tests use synthetic fixtures | Accountability evidence; not a substitute for deployment checks or a legal assessment |

## Content rules

Do not put credentials, government identifiers, payment-card information, protected health information or personal information about children below the applicable digital-consent age into the hosted service or plugin. The Netherlands uses 16 where consent rules apply. Keep other special-category information out unless a separately supported lawful process permits it. Do not infer such permission from a project selection, an AI request or an earlier approval of another record.

Content may concern someone other than the uploader. Upload authorization does not automatically establish that person's consent, a lawful basis, an Article 9 condition, or copyright permission. A scanner cannot reliably infer those rights or determine age. Ordinary discussions of medicine, security, parenting or regulation are not automatically personal records; blanket keyword bans would misrepresent that distinction. Minimize what reaches Cloud and connected AI providers.

## Operator requirements

Before broad hosted launch, maintain a private processing register, DPIA screening/full assessment where required, supplier/transfer contract evidence, incident register and rights-request process. Confirm registration/accounting obligations and the actual contracting entity. Customer-facing terms must be fairly disclosed and lawfully incorporated; a proposed liability cap is not effective merely because it is written in a repository.

The source licence's warranty provisions do not silently amend OpenAI or Paddle agreements. No universal personal-liability ceiling, GDPR certification, end-to-end encryption claim or guarantee of complete sensitive-content recognition is made. The server can decrypt approved Cloud history to answer authorized requests. Copies delivered to a selected AI provider are governed by that provider's applicable terms as well as any independent legal obligations.

Sources: [OpenAI developer terms](https://openai.com/en-GB/policies/developer-apps-terms/), [plugin guidelines](https://developers.openai.com/plugins/plugin-guidelines), [EDPB data protection guide](https://www.edpb.europa.eu/sme_en), [Dutch general-conditions guidance](https://business.gov.nl/regulations/general-terms-conditions/). See `docs/PRIVACY.md` for the processing notice and deployed retention descriptions. Legal conclusions, executed contracts and actual production settings require separate evidence.
