/**
 * The version this bundle was built as: `frontend/package.json` `version`, injected by Vite
 * `define` (the Docker frontend stage has no backend files; scripts/check-docs.sh keeps it equal
 * to the backend version). Compared with `/api/health` `version` by the UpdateBanner.
 */
export const BUILD_VERSION: string = __COPPERCHAT_VERSION__;
