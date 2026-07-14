import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=58380)
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        raise SystemExit("Literature Workbench may bind only to 127.0.0.1")
    uvicorn.run(
        "literature_workbench.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        forwarded_allow_ips="",
    )


if __name__ == "__main__":
    main()
