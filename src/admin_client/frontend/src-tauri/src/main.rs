// Предотвращает появление дополнительного окна консоли в Windows в release-режиме сборки.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
  app_lib::run();
}
