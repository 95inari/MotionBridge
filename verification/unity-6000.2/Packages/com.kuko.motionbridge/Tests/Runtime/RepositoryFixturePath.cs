using System;
using System.IO;
using UnityEditor.PackageManager;

namespace Kuko.MotionBridge.Tests
{
    internal static class RepositoryFixturePath
    {
        internal static string Find(params string[] pathUnderProtocol)
        {
            var package = PackageInfo.FindForAssembly(typeof(RepositoryFixturePath).Assembly);
            if (package == null)
                throw new InvalidOperationException("Motion Bridge package was not resolved.");

            var directory = new DirectoryInfo(package.resolvedPath);
            while (directory != null)
            {
                var candidate = Path.Combine(directory.FullName, "protocol");
                foreach (var component in pathUnderProtocol)
                    candidate = Path.Combine(candidate, component);
                if (File.Exists(candidate))
                    return candidate;
                directory = directory.Parent;
            }

            throw new FileNotFoundException(
                $"Repository protocol fixture was not found: {Path.Combine(pathUnderProtocol)}");
        }
    }
}
