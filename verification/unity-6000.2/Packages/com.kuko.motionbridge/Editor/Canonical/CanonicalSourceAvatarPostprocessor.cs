using System;
using System.IO;
using UnityEditor;

namespace Kuko.MotionBridge.Editor
{
    internal sealed class CanonicalSourceAvatarPostprocessor : AssetPostprocessor
    {
        internal const string AssetName = "MB_Canonical_Humanoid_v1";

        private void OnPreprocessModel()
        {
            if (!string.Equals(
                    Path.GetFileNameWithoutExtension(assetPath),
                    AssetName,
                    StringComparison.OrdinalIgnoreCase) ||
                assetImporter is not ModelImporter importer)
                return;

            importer.animationType = ModelImporterAnimationType.Human;
            importer.avatarSetup = ModelImporterAvatarSetup.CreateFromThisModel;
            importer.bakeAxisConversion = false;
            importer.importAnimation = false;
            importer.importCameras = false;
            importer.importLights = false;
        }
    }
}
