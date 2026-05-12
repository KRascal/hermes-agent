from pathlib import Path


def test_deploy_site_cancels_superseded_pages_builds():
    text = Path(".github/workflows/deploy-site.yml").read_text()

    assert "group: pages-${{ github.ref }}" in text
    assert "cancel-in-progress: true" in text
