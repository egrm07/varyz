"""Print Shared Varyz collaboration counts (no content) for later pricing decisions.

Run where the Cloud database lives, with the same environment as the service.
"""

import json

from varyz.cloud.app import Settings, create_app


def main():
    app = create_app(Settings.from_env())
    print(json.dumps(app.state.sharing.metrics(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
