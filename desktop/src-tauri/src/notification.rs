//! 等待 macOS 的实际通知提交回执，避免无窗口进程提前退出。

use block2::{DynBlock, RcBlock};
use objc2::{define_class, msg_send, rc::Retained, runtime::ProtocolObject, ClassType};
use objc2_foundation::{NSError, NSObject, NSObjectProtocol, NSString};
use objc2_user_notifications::{
    UNAuthorizationStatus, UNMutableNotificationContent, UNNotification,
    UNNotificationPresentationOptions, UNNotificationRequest, UNNotificationSettings,
    UNNotificationSound, UNUserNotificationCenter, UNUserNotificationCenterDelegate,
};
use std::{
    ptr::NonNull,
    sync::{
        mpsc::{self, Receiver},
        Once,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

define_class!(
    #[unsafe(super(NSObject))]
    #[name = "SecondProprioNotificationDelegate"]
    struct NotificationDelegate;

    unsafe impl NSObjectProtocol for NotificationDelegate {}

    unsafe impl UNUserNotificationCenterDelegate for NotificationDelegate {
        #[unsafe(method(userNotificationCenter:willPresentNotification:withCompletionHandler:))]
        fn will_present(
            &self,
            _center: &UNUserNotificationCenter,
            _notification: &UNNotification,
            completion: &DynBlock<dyn Fn(UNNotificationPresentationOptions)>,
        ) {
            completion.call((UNNotificationPresentationOptions::Banner
                | UNNotificationPresentationOptions::List
                | UNNotificationPresentationOptions::Sound,));
        }
    }
);

/// 为当前进程安装前台展示代理，实际展示仍遵循系统通知和专注模式设置。
pub fn install_foreground_delegate() {
    static INSTALLED: Once = Once::new();
    INSTALLED.call_once(|| {
        // NSObject 的 new 返回本类已初始化的强引用；代理无可变状态。
        let delegate: Retained<NotificationDelegate> =
            unsafe { msg_send![NotificationDelegate::class(), new] };
        UNUserNotificationCenter::currentNotificationCenter()
            .setDelegate(Some(ProtocolObject::from_ref(&*delegate)));
        // 系统仅弱引用 delegate；每个进程只创建一次，并保留到进程退出。
        std::mem::forget(delegate);
    });
}

fn await_reply<T>(receiver: Receiver<T>, deadline: Instant) -> Result<T, String> {
    receiver
        .recv_timeout(deadline.saturating_duration_since(Instant::now()))
        .map_err(|_| "macOS 通知回执超时，尚未确认提交成功".to_string())
}

fn check_permission(status: UNAuthorizationStatus) -> Result<(), String> {
    match status {
        UNAuthorizationStatus::Authorized | UNAuthorizationStatus::Provisional => Ok(()),
        UNAuthorizationStatus::Denied => {
            Err("macOS 已关闭通知权限，请在系统设置中允许 second proprio 通知".into())
        }
        _ => Err("macOS 尚未授予通知权限，请在系统设置中允许 second proprio 通知".into()),
    }
}

/// 在后台线程提交通知；主线程继续处理原生事件，成功仅代表系统已接受请求。
pub fn submit(title: &str, body: &str) -> Result<(), String> {
    let deadline = Instant::now() + Duration::from_secs(8);
    install_foreground_delegate();
    let center = UNUserNotificationCenter::currentNotificationCenter();
    let (sender, receiver) = mpsc::sync_channel(1);
    let settings_reply = RcBlock::new(move |settings: NonNull<UNNotificationSettings>| {
        // 系统保证参数在回调期间有效，只把可复制的授权状态送回工作线程。
        let status = unsafe { settings.as_ref() }.authorizationStatus();
        let _ = sender.send(status);
    });
    center.getNotificationSettingsWithCompletionHandler(&settings_reply);
    check_permission(await_reply(receiver, deadline)?)?;

    let content = UNMutableNotificationContent::new();
    content.setTitle(&NSString::from_str(title));
    content.setBody(&NSString::from_str(body));
    content.setSound(Some(&UNNotificationSound::defaultSound()));
    let stamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    let identifier = NSString::from_str(&format!("second-proprio-{}-{stamp}", std::process::id()));
    let request =
        UNNotificationRequest::requestWithIdentifier_content_trigger(&identifier, &content, None);
    let (sender, receiver) = mpsc::sync_channel(1);
    let submission_reply = RcBlock::new(move |error: *mut NSError| {
        // 错误指针可为空；在系统回调返回前复制诊断文本，不跨线程保存原生引用。
        let result = match unsafe { error.as_ref() } {
            Some(error) => Err(format!(
                "{} ({}:{})",
                error.localizedDescription(),
                error.domain(),
                error.code()
            )),
            None => Ok(()),
        };
        let _ = sender.send(result);
    });
    center.addNotificationRequest_withCompletionHandler(&request, Some(&submission_reply));
    await_reply(receiver, deadline)?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_missing_or_denied_permission() {
        assert!(check_permission(UNAuthorizationStatus::NotDetermined).is_err());
        assert!(check_permission(UNAuthorizationStatus::Denied).is_err());
        assert!(check_permission(UNAuthorizationStatus::Authorized).is_ok());
    }

    #[test]
    fn missing_callback_is_not_accepted() {
        let (_sender, receiver) = mpsc::sync_channel::<()>(1);
        assert!(await_reply(receiver, Instant::now()).is_err());
    }

    #[test]
    fn preserves_system_submission_error() {
        let (sender, receiver) = mpsc::sync_channel(1);
        sender
            .send(Err::<(), _>("系统拒绝通知".to_string()))
            .unwrap();
        assert_eq!(
            await_reply(receiver, Instant::now() + Duration::from_secs(1)).unwrap(),
            Err("系统拒绝通知".to_string())
        );
    }
}
